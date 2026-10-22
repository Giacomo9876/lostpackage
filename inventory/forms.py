from datetime import date
from decimal import Decimal

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import ocr
from .images import process_receipt, process_upload
from .models import Batch, DayClose, Package, ShopSettings

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

# Il prezzo di vendita si può scrivere a mano (sconti, prezzi concordati), ma entro limiti ragionevoli:
# un errore di battitura come 5000 al posto di 50 non deve finire nel fatturato.
MAX_SALE_PRICE = Decimal("9999.99")


def check_sale_price(value):
    if value is not None and not Decimal("0") <= value <= MAX_SALE_PRICE:
        raise ValidationError("Il prezzo deve essere tra 0 e 9999,99 €.")
    return value


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

    Il prezzo parte da quello delle regole (peso, premium, pacco piccolo) ma chi vende può
    scriverne un altro: uno sconto, un prezzo concordato.
    """

    class Meta:
        model = Package
        fields = ["sold_price", "payment"]
        labels = {"sold_price": "Prezzo di vendita", "payment": "Pagamento"}
        localized_fields = ["sold_price"]
        widgets = {"sold_price": forms.TextInput(attrs=MONEY_WIDGET_ATTRS), "payment": forms.RadioSelect}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sold_price"].required = True

    def clean_sold_price(self):
        return check_sale_price(self.cleaned_data["sold_price"])


class ClosingEntryForm(forms.Form):
    """Un pacco venduto, inserito a fine giornata: peso, premium, pagamento e (se serve) il prezzo."""

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
    payment = forms.ChoiceField(
        label="Pagamento",
        choices=Package.Payment.choices,
        initial=Package.Payment.CASH,
        widget=forms.RadioSelect,
    )
    # Vuoto = prezzo calcolato dalle regole. Il browser lo precompila e lo aggiorna da solo.
    price = forms.DecimalField(
        label="Prezzo",
        required=False,
        max_digits=7,
        decimal_places=2,
        localize=True,
        widget=forms.TextInput(attrs=MONEY_WIDGET_ATTRS),
    )

    def clean_price(self):
        return check_sale_price(self.cleaned_data["price"])


MAX_CLOSE_AMOUNT = Decimal("9999999.99")


# Una giornata più vecchia di così è quasi certamente un anno sbagliato (0202, 2002…).
EARLIEST_CLOSE_DAY = date(2020, 1, 1)


class CloseDayForm(forms.Form):
    """La chiusura di una giornata: scontrini battuti (scritti o letti dalla foto) e contanti Yakuza.

    L'importo degli scontrini si può scrivere, oppure lasciare vuoto con la foto dello scontrino: allora
    lo si legge dalla foto (se la lettura è disponibile). Quello che resta salvato è sempre l'importo
    confermato da chi chiude; `detected_total` conserva ciò che era stato letto, per vedere le correzioni.

    `day` serve solo al titolare che aggiunge una giornata: la sceglie qui, dentro al modulo, così il giorno
    che vede è quello che viene salvato. Se usarlo (e come controllarlo) lo decide la vista.
    """

    day = forms.DateField(
        label="Giornata dello scontrino",
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )

    receipts_total = forms.DecimalField(
        label="Totale degli scontrini (€)",
        required=False,
        min_value=Decimal("0"),
        max_value=MAX_CLOSE_AMOUNT,
        max_digits=10,
        decimal_places=2,
        localize=True,
        widget=forms.TextInput(attrs=MONEY_WIDGET_ATTRS),
    )
    yakuza_cash = forms.DecimalField(
        label="Contanti Yakuza (€, facoltativi)",
        required=False,
        min_value=Decimal("0"),
        max_value=MAX_CLOSE_AMOUNT,
        max_digits=10,
        decimal_places=2,
        localize=True,
        help_text="Contanti incassati senza scontrino, in più rispetto agli scontrini.",
        widget=forms.TextInput(attrs=MONEY_WIDGET_ATTRS),
    )
    photo = forms.FileField(
        label="Foto dello scontrino (facoltativa)",
        required=False,
        # "capture" apre direttamente la fotocamera posteriore sul telefono.
        widget=forms.FileInput(attrs={"accept": "image/*", "capture": "environment"}),
    )
    remove_photo = forms.BooleanField(label="Togli la foto", required=False)
    reading_token = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["day"].widget.attrs.update(min=EARLIEST_CLOSE_DAY.isoformat(), max=timezone.localdate().isoformat())
        self._receipt = None  # la foto nuova, già ricodificata (ContentFile JPEG)
        self.detected = None  # importo letto dalla nuova foto
        self.filled_from_photo = False  # l'importo è stato preso dalla foto perché non era scritto

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if isinstance(photo, UploadedFile):
            limit = settings.MAX_UPLOAD_MB
            if photo.size > limit * 1024 * 1024:
                raise ValidationError(f"La foto supera {limit} MB. Riducila o scattala di nuovo.")
            self._receipt = process_receipt(photo)
        return photo

    def _reading(self, data, token):
        """Cosa dice la foto: dal gettone di una lettura già fatta su questa stessa foto, o leggendola ora."""
        if token:
            valid, reading = ocr.verify_reading(token, self.user.pk, data)
            if valid:
                return reading
        return ocr.read_total(data)

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        if self._receipt is not None:
            data = self._receipt.read()
            self._receipt.seek(0)
            reading = self._reading(data, cleaned.get("reading_token"))
            self.detected = reading.amount if reading else None
            if cleaned.get("receipts_total") is None and reading:
                cleaned["receipts_total"] = reading.amount
                self.filled_from_photo = True
        if cleaned.get("receipts_total") is None:
            if self._receipt is not None and ocr.available():
                message = "Non riesco a leggere l'importo dalla foto: scrivilo a mano."
            else:
                message = "Scrivi il totale degli scontrini."
            self.add_error("receipts_total", message)
        cleaned["yakuza_cash"] = cleaned.get("yakuza_cash") or Decimal("0.00")
        return cleaned

    def save(self, store, day):
        """Salva (o aggiorna) la chiusura di quel centro e giorno; ritorna l'oggetto DayClose."""
        data = self.cleaned_data
        for attempt in (1, 2):
            close = DayClose.objects.filter(store=store, date=day).first() or DayClose(store=store, date=day)
            old = [(close.photo.storage, close.photo.name)] if close.photo else []
            close.receipts_total = data["receipts_total"]
            close.yakuza_cash = data["yakuza_cash"]
            close.closed_by = self.user
            replaced = []
            if self._receipt is not None:
                close.photo.save("scontrino.jpg", self._receipt, save=False)
                close.detected_total = self.detected
                replaced = old
            elif data["remove_photo"] and close.photo:
                close.photo = ""
                close.detected_total = None
                replaced = old
            try:
                with transaction.atomic():
                    close.save()
            except IntegrityError:
                # Due invii quasi insieme: l'altro ha già creato la riga. Il file nuovo non serve più; si riprova una volta.
                if close.photo and self._receipt is not None:
                    close.photo.storage.delete(close.photo.name)
                    self._receipt.seek(0)
                if attempt == 2:
                    raise
                continue
            for storage, name in replaced:
                transaction.on_commit(lambda storage=storage, name=name: storage.delete(name))
            return close


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
