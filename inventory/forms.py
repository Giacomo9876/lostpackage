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
    class Meta:
        model = Package
        fields = ["photo", "weight_kg", "batch", "notes"]
        localized_fields = ["weight_kg"]
        labels = {"photo": "Foto del pacco", "batch": "Lotto"}
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Ogni pacco appartiene a un lotto: il lotto si crea prima, poi si registrano i pacchi.
        self.fields["batch"].required = True
        self.fields["batch"].empty_label = "Scegli il lotto"
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

    class Meta:
        model = Batch
        fields = ["name", "expected_count"]
        labels = {"name": "Nome del lotto"}
        widgets = {"name": forms.TextInput(attrs={"placeholder": "Es. Lotto 1", "autocomplete": "off"})}
        error_messages = {"name": {"unique": "Esiste già un lotto con questo nome."}}

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
        duplicates = Batch.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise ValidationError("Esiste già un lotto con questo nome.")
        return name


class SaleForm(forms.ModelForm):
    """Conferma della vendita: il prezzo è precompilato dal peso, ma si può cambiare."""

    class Meta:
        model = Package
        fields = ["sold_price"]
        labels = {"sold_price": "Prezzo di vendita"}
        localized_fields = ["sold_price"]
        widgets = {"sold_price": forms.TextInput(attrs=MONEY_WIDGET_ATTRS)}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sold_price"].required = True


class PriceForm(forms.ModelForm):
    class Meta:
        model = ShopSettings
        fields = ["price_per_kg"]
        labels = {"price_per_kg": "Prezzo al kg"}
        localized_fields = ["price_per_kg"]
        widgets = {"price_per_kg": forms.TextInput(attrs=MONEY_WIDGET_ATTRS)}


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
    batch = forms.ModelChoiceField(
        label="Lotto",
        required=False,
        queryset=Batch.objects.all(),
        empty_label="Tutti i lotti",
    )
