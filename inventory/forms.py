from decimal import Decimal

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile

from .images import process_upload
from .models import Batch, Package, ShopSettings

TOLERANCE_CHOICES = [
    (0, "Peso esatto"),
    (25, "± 25 g"),
    (50, "± 50 g"),
    (100, "± 100 g"),
    (250, "± 250 g"),
    (500, "± 500 g"),
]
DEFAULT_TOLERANCE_G = 50

MONEY_WIDGET_ATTRS = {"inputmode": "decimal", "placeholder": "0,00", "autocomplete": "off"}


class PackageForm(forms.ModelForm):
    """Registra o modifica un pacco di un centro. Foto e lotto sono facoltativi.

    Su un pacco già venduto peso e premium non si toccano: il prezzo è già stato fissato
    (per correggere il peso si rimette il pacco in magazzino).
    """

    class Meta:
        model = Package
        fields = ["photo", "weight_kg", "premium", "batch", "notes"]
        localized_fields = ["weight_kg"]
        labels = {"photo": "Foto del pacco (facoltativa)", "batch": "Lotto (facoltativo)"}
        widgets = {
            # "capture" apre direttamente la fotocamera posteriore sul telefono.
            "photo": forms.FileInput(attrs={"accept": "image/*", "capture": "environment"}),
            "weight_kg": forms.TextInput(
                attrs={"inputmode": "decimal", "placeholder": "0,000", "autocomplete": "off"}
            ),
            "notes": forms.TextInput(
                attrs={"placeholder": "Es. scatola Amazon, etichetta rovinata", "autocomplete": "off"}
            ),
        }
        help_texts = {"notes": "Facoltative."}

    def __init__(self, *args, store, **kwargs):
        super().__init__(*args, **kwargs)
        # Si può scegliere solo tra i lotti del centro del pacco.
        self.fields["batch"].queryset = Batch.objects.filter(store=store)
        self.fields["batch"].empty_label = "Nessun lotto"
        if self.instance.is_sold:
            del self.fields["weight_kg"]
            del self.fields["premium"]
        self._images = None
        # Vanno ricordati qui: durante la validazione ModelForm sostituisce già
        # instance.photo con il nuovo file, e i vecchi nomi andrebbero persi.
        self._old_files = []
        if self.instance.pk:
            self._old_files = [
                (f.storage, f.name) for f in (self.instance.photo, self.instance.thumb) if f and f.name
            ]

    def clean_photo(self):
        photo = self.cleaned_data["photo"]
        # Su modifica senza nuova foto arriva il file già salvato: niente da rielaborare.
        if isinstance(photo, UploadedFile):
            limit = settings.MAX_UPLOAD_MB
            if photo.size > limit * 1024 * 1024:
                raise ValidationError(f"La foto supera {limit} MB. Riducila o scattala di nuovo.")
            self._images = process_upload(photo)
        return photo

    def save(self, commit=True):
        package = super().save(commit=False)

        replaced = []
        if self._images:
            photo, thumb = self._images
            package.photo.save("foto.jpg", photo, save=False)
            package.thumb.save("miniatura.jpg", thumb, save=False)
            replaced = self._old_files

        if commit:
            package.save()
            for storage, file_name in replaced:
                storage.delete(file_name)
        return package


class BatchForm(forms.ModelForm):
    expected_count = forms.IntegerField(
        label="Numero di pacchi del lotto (se lo sai)",
        help_text=(
            "Serve a vedere a che punto sei. Se non lo sai ancora, lascialo vuoto: "
            "puoi indicarlo o cambiarlo quando vuoi, anche mentre registri."
        ),
        required=False,
        min_value=1,
        max_value=2000,
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "placeholder": "Es. 10, oppure vuoto"}),
    )

    def __init__(self, *args, store, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = store

    class Meta:
        model = Batch
        fields = ["name", "expected_count"]
        labels = {"name": "Nome del lotto"}
        widgets = {"name": forms.TextInput(attrs={"placeholder": "Es. Lotto 1", "autocomplete": "off"})}

    def clean_expected_count(self):
        # Vuoto = "non lo so ancora": si salva 0, che l'app tratta come totale non indicato.
        value = self.cleaned_data.get("expected_count") or 0
        if value and self.instance.pk:
            registered = self.instance.packages.count()
            if value < registered:
                raise ValidationError(
                    f"Hai già registrato {registered} pacchi in questo lotto: il totale non può essere più basso."
                )
        return value

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        # Il nome è unico dentro il centro: un altro centro può avere lo stesso «Lotto 1».
        duplicates = Batch.objects.filter(store=self.store, name__iexact=name).exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise ValidationError("In questo centro esiste già un lotto con questo nome.")
        return name


class SaleForm(forms.ModelForm):
    """Conferma della vendita dal magazzino.

    Il prezzo lo calcolano le regole (peso, premium, pacco piccolo). Solo i titolari possono
    cambiarlo; per lo store manager il campo non esiste.
    """

    class Meta:
        model = Package
        fields = ["sold_price", "receipt"]
        labels = {"sold_price": "Prezzo di vendita", "receipt": "Scontrino emesso"}
        localized_fields = ["sold_price"]
        widgets = {"sold_price": forms.TextInput(attrs=MONEY_WIDGET_ATTRS)}

    def __init__(self, *args, can_set_price, **kwargs):
        super().__init__(*args, **kwargs)
        if can_set_price:
            self.fields["sold_price"].required = True
        else:
            del self.fields["sold_price"]


class ClosingEntryForm(forms.Form):
    """Un pacco venduto, inserito a fine giornata: peso, premium e scontrino."""

    weight_kg = forms.DecimalField(
        label="Peso",
        min_value=Decimal("0.001"),
        max_value=Decimal("1000"),
        max_digits=7,
        decimal_places=3,
        localize=True,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "placeholder": "0,000", "autocomplete": "off"}),
    )
    premium = forms.BooleanField(label="Pacco premium", required=False)
    receipt = forms.BooleanField(label="Scontrino emesso", required=False, initial=True)


class CloseForm(forms.Form):
    """Totale degli scontrini battuti in cassa nella giornata."""

    receipts_total = forms.DecimalField(
        label="Totale scontrini battuti in cassa",
        min_value=Decimal("0"),
        max_value=Decimal("9999999.99"),
        max_digits=10,
        decimal_places=2,
        localize=True,
        widget=forms.TextInput(attrs=MONEY_WIDGET_ATTRS),
    )


class PriceForm(forms.ModelForm):
    class Meta:
        model = ShopSettings
        fields = ["price_per_kg", "premium_price_per_kg", "small_parcel_max_g", "small_parcel_price"]
        labels = {
            "price_per_kg": "Prezzo al kg",
            "premium_price_per_kg": "Prezzo al kg dei pacchi premium",
            "small_parcel_max_g": "Pacchi piccoli: fino a (grammi)",
            "small_parcel_price": "Pacchi piccoli: prezzo fisso",
        }
        help_texts = {
            "small_parcel_max_g": "Fino a questo peso ogni pacco costa il prezzo fisso, premium compresi.",
        }
        localized_fields = ["price_per_kg", "premium_price_per_kg", "small_parcel_price"]
        widgets = {
            "price_per_kg": forms.TextInput(attrs=MONEY_WIDGET_ATTRS),
            "premium_price_per_kg": forms.TextInput(attrs=MONEY_WIDGET_ATTRS),
            "small_parcel_price": forms.TextInput(attrs=MONEY_WIDGET_ATTRS),
            "small_parcel_max_g": forms.NumberInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
        }


class BatchChoiceField(forms.ModelChoiceField):
    """Per i titolari il lotto si riconosce dal centro: «Lotto 1 · Globo»."""

    show_store = False

    def label_from_instance(self, obj):
        return f"{obj.name} · {obj.store.name}" if self.show_store else obj.name


class SearchForm(forms.Form):
    peso = forms.DecimalField(
        label="Cerca per peso",
        required=False,
        min_value=Decimal("0"),
        max_digits=7,
        decimal_places=3,
        localize=True,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "placeholder": "0,000", "autocomplete": "off"}),
    )
    tolleranza = forms.TypedChoiceField(
        label="Tolleranza",
        required=False,
        choices=TOLERANCE_CHOICES,
        coerce=int,
        empty_value=DEFAULT_TOLERANCE_G,
        initial=DEFAULT_TOLERANCE_G,
    )
    batch = BatchChoiceField(label="Lotto", required=False, queryset=Batch.objects.none(), empty_label="Tutti i lotti")

    def __init__(self, *args, scope, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["batch"].queryset = scope.batches().select_related("store")
        self.fields["batch"].show_store = scope.is_owner
        if scope.is_owner:
            # Solo i titolari vedono più centri, quindi solo loro possono filtrare per centro.
            self.fields["centro"] = forms.ModelChoiceField(
                label="Centro", required=False, queryset=scope.stores(), empty_label="Tutti i centri"
            )
            self.order_fields(["peso", "tolleranza", "centro", "batch"])
