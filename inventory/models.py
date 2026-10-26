import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

CENT = Decimal("0.01")


def photo_upload_path(instance, filename):
    # Il nome originale del file non viene mai usato: solo un UUID casuale.
    return f"packages/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.jpg"


def thumb_upload_path(instance, filename):
    return f"packages/{timezone.now():%Y/%m}/thumbs/{uuid.uuid4().hex}.jpg"


def receipt_upload_path(instance, filename):
    return f"receipts/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.jpg"


class Store(models.Model):
    """Un centro commerciale dove c'è un negozio."""

    name = models.CharField("Centro commerciale", max_length=80, unique=True)
    city = models.CharField("Città", max_length=80, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "centro commerciale"
        verbose_name_plural = "centri commerciali"

    def __str__(self):
        return self.name

    @property
    def label(self):
        return f"{self.name} · {self.city}" if self.city else self.name


class Franchise(models.Model):
    """Un punto in franchising: un negozio affiliato con i suoi utenti e i suoi numeri.

    È separato dai centri commerciali: i suoi incassi non entrano nella dashboard né nelle medie dei
    centri, e chi lavora in un franchising vede solo il proprio punto.
    """

    name = models.CharField("Nome del punto", max_length=80, unique=True)
    city = models.CharField("Città", max_length=80, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "punto in franchising"
        verbose_name_plural = "punti in franchising"

    def __str__(self):
        return self.name

    @property
    def label(self):
        return f"{self.name} · {self.city}" if self.city else self.name


class Profile(models.Model):
    """Ruolo di un utente: i titolari vedono tutti i centri, gli store manager solo il proprio,
    chi è in franchising solo il proprio punto in franchising (e nient'altro dell'app).

    Un utente senza profilo (e che non sia superuser) non vede niente: nessun accesso per caso.
    """

    class Role(models.TextChoices):
        OWNER = "owner", "Titolare (vede tutti i centri)"
        MANAGER = "manager", "Store manager (vede solo il suo centro)"
        FRANCHISE = "franchise", "Franchising (vede solo il suo punto)"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField("Ruolo", max_length=10, choices=Role.choices, default=Role.MANAGER)
    store = models.ForeignKey(
        Store,
        verbose_name="Centro commerciale",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staff",
        help_text="Obbligatorio per gli store manager. Per i titolari non serve.",
    )
    franchise = models.ForeignKey(
        Franchise,
        verbose_name="Punto in franchising",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staff",
        help_text="Obbligatorio per il ruolo Franchising.",
    )

    class Meta:
        verbose_name = "profilo"
        verbose_name_plural = "profili"
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=["owner", "franchise"]) | Q(store__isnull=False), name="manager_has_a_store"
            ),
            models.CheckConstraint(
                condition=Q(role__in=["owner", "manager"]) | Q(franchise__isnull=False),
                name="franchisee_has_a_franchise",
            ),
        ]

    def __str__(self):
        return f"{self.user} – {self.get_role_display()}"

    @property
    def is_owner(self):
        return self.role == self.Role.OWNER

    def clean(self):
        if self.role == self.Role.MANAGER and not self.store_id:
            raise ValidationError({"store": "Uno store manager deve avere un centro commerciale."})
        if self.role == self.Role.FRANCHISE and not self.franchise_id:
            raise ValidationError({"franchise": "Chi è in franchising deve avere il suo punto."})

    def save(self, *args, **kwargs):
        # Ogni ruolo tiene solo il legame che gli serve: un centro o un punto, mai entrambi.
        if self.role != self.Role.MANAGER:
            self.store = None
        if self.role != self.Role.FRANCHISE:
            self.franchise = None
        super().save(*args, **kwargs)


class ShopSettings(models.Model):
    """Impostazioni dei prezzi. Esiste una sola riga.

    Regole: fino a `small_parcel_max_g` grammi ogni pacco costa `small_parcel_price`;
    oltre, peso × prezzo al kg (quello premium per i pacchi premium).
    """

    price_per_kg = models.DecimalField(
        "Prezzo al kg (€)",
        max_digits=7,
        decimal_places=2,
        default=Decimal("25.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    premium_price_per_kg = models.DecimalField(
        "Prezzo al kg dei pacchi premium (€)",
        max_digits=7,
        decimal_places=2,
        default=Decimal("30.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    small_parcel_max_g = models.PositiveIntegerField(
        "Pacchi piccoli: fino a (g)",
        default=200,
        validators=[MaxValueValidator(5000)],
    )
    small_parcel_price = models.DecimalField(
        "Pacchi piccoli: prezzo fisso (€)",
        max_digits=7,
        decimal_places=2,
        default=Decimal("5.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "impostazioni"
        verbose_name_plural = "impostazioni"

    @classmethod
    def load(cls):
        settings_row, _ = cls.objects.get_or_create(pk=1)
        return settings_row

    def price_for(self, weight_kg, premium=False):
        """L'unico posto dove si calcola il prezzo di un pacco, arrotondato al centesimo."""
        weight = Decimal(weight_kg)
        if weight <= Decimal(self.small_parcel_max_g) / 1000:
            return Decimal(self.small_parcel_price).quantize(CENT, rounding=ROUND_HALF_UP)
        rate = self.premium_price_per_kg if premium else self.price_per_kg
        return (weight * rate).quantize(CENT, rounding=ROUND_HALF_UP)


class Batch(models.Model):
    """Un lotto di pacchi arrivati insieme a un centro, con il numero di pacchi che ci si aspetta.

    Il nome è unico dentro il centro: due centri possono avere ciascuno il proprio «Lotto 1».
    """

    store = models.ForeignKey(Store, verbose_name="Centro", on_delete=models.PROTECT, related_name="batches")
    name = models.CharField("Nome", max_length=80)
    # 0 = non indicato (lotti creati prima che esistesse questo campo).
    expected_count = models.PositiveIntegerField("Numero di pacchi", default=0)
    created_at = models.DateTimeField("Creato il", auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "lotto"
        verbose_name_plural = "lotti"
        constraints = [models.UniqueConstraint(fields=["store", "name"], name="unique_batch_name_per_store")]

    def __str__(self):
        return self.name


class PackageQuerySet(models.QuerySet):
    def in_stock(self):
        return self.filter(sold_at__isnull=True)

    def sold(self):
        return self.filter(sold_at__isnull=False)


class Package(models.Model):
    """Un pacco. Finché `sold_at` è vuoto è in magazzino; venduto finisce nello storico."""

    class Payment(models.TextChoices):
        """Come è stato pagato. Tutti hanno lo scontrino tranne «Yakuza», il venduto in nero."""

        CASH = "cash", "Contanti"
        CARD = "card", "Carta"
        OTHER = "other", "Altro"
        YAKUZA = "yakuza", "Yakuza"

    store = models.ForeignKey(Store, verbose_name="Centro", on_delete=models.PROTECT, related_name="packages")
    batch = models.ForeignKey(
        Batch,
        verbose_name="Lotto",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="packages",
    )
    weight_kg = models.DecimalField(
        "Peso (kg)",
        max_digits=7,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001")), MaxValueValidator(Decimal("1000"))],
    )
    premium = models.BooleanField("Pacco premium", default=False)
    # La foto è facoltativa: a fine giornata nessuno ha il tempo di fotografare ogni pacco.
    photo = models.ImageField("Foto", upload_to=photo_upload_path, max_length=255, blank=True)
    thumb = models.ImageField(upload_to=thumb_upload_path, max_length=255, editable=False, blank=True)
    notes = models.CharField("Note", max_length=255, blank=True)
    created_at = models.DateTimeField("Registrato il", auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    # Vendita. Il prezzo si salva al momento della vendita: cambiare il prezzo al kg
    # in seguito non altera lo storico.
    sold_at = models.DateTimeField("Venduto il", null=True, blank=True)
    sold_price = models.DecimalField(
        "Prezzo di vendita (€)",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    sold_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    # Prezzo secondo le regole nel momento della vendita. Chi vende può scrivere un prezzo diverso
    # (sconto, prezzo concordato): la differenza con `sold_price` è lo sconto. Vuoto per le vendite
    # registrate prima che esistesse questo campo.
    list_price = models.DecimalField(
        "Prezzo da listino (€)", max_digits=8, decimal_places=2, null=True, blank=True
    )
    # Metodo di pagamento alla vendita. Il fatturato dei pacchi «Yakuza» è la voce Yakuza della dashboard;
    # per gli altri metodi lo scontrino c'è.
    payment = models.CharField("Pagamento", max_length=10, choices=Payment.choices, default=Payment.CASH)

    objects = PackageQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["weight_kg"]),
            models.Index(fields=["sold_at"]),
            models.Index(fields=["store", "sold_at"]),
        ]
        verbose_name = "pacco"
        verbose_name_plural = "pacchi"

    def __str__(self):
        return f"Pacco #{self.pk} ({self.weight_kg} kg)"

    @property
    def is_sold(self):
        return self.sold_at is not None

    @property
    def has_photo(self):
        return bool(self.photo)

    @property
    def is_yakuza(self):
        return self.payment == self.Payment.YAKUZA

    @property
    def receipt(self):
        """Lo scontrino c'è per ogni pagamento tranne Yakuza."""
        return not self.is_yakuza

    def price_with(self, shop):
        """Prezzo in base alle regole correnti (peso, premium, pacco piccolo)."""
        return shop.price_for(self.weight_kg, self.premium)

    @property
    def price_adjustment(self):
        """Quanto il prezzo di vendita si discosta dal listino (negativo = sconto); None se uguale o ignoto."""
        if self.sold_price is None or self.list_price is None:
            return None
        return (self.sold_price - self.list_price) or None


class ReceiptDay(models.Model):
    """Ciò che hanno in comune una chiusura di un centro e una giornata di un franchising.

    L'importo degli scontrini si scrive a mano oppure si legge dalla foto dello scontrino di chiusura
    (`detected_total`): in ogni caso `receipts_total` è quello confermato da chi chiude, e può
    correggere ciò che è stato letto. Le medie (infrasettimanale, settimanale, fine settimana) si
    calcolano da queste giornate.
    """

    date = models.DateField("Giornata")
    receipts_total = models.DecimalField(
        "Totale scontrini battuti (€)",
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    yakuza_cash = models.DecimalField(
        "Yakuza (€)",
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    photo = models.ImageField("Foto dello scontrino", upload_to=receipt_upload_path, max_length=255, blank=True)
    detected_total = models.DecimalField(
        "Importo letto dalla foto (€)", max_digits=10, decimal_places=2, null=True, blank=True
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    @property
    def has_photo(self):
        return bool(self.photo)

    @property
    def day_total(self):
        """Incasso della giornata: scontrini più Yakuza."""
        return self.receipts_total + self.yakuza_cash

    @property
    def reading(self):
        """Com'è stato ottenuto l'importo: «typed» (scritto), «read» (letto dalla foto), «corrected» (letto e poi corretto)."""
        if self.detected_total is None:
            return "typed"
        return "read" if self.detected_total == self.receipts_total else "corrected"


class DayClose(ReceiptDay):
    """Chiusura di una giornata in un centro: gli scontrini battuti in cassa e lo Yakuza."""

    store = models.ForeignKey(Store, verbose_name="Centro", on_delete=models.PROTECT, related_name="closes")

    class Meta:
        ordering = ["-date", "store__name"]
        verbose_name = "chiusura di giornata"
        verbose_name_plural = "chiusure di giornata"
        constraints = [models.UniqueConstraint(fields=["store", "date"], name="one_close_per_store_per_day")]

    def __str__(self):
        return f"{self.store} – {self.date:%d/%m/%Y}"


class FranchiseDay(ReceiptDay):
    """Una giornata di un punto in franchising: scontrini e Yakuza come i centri, più contanti e carta,
    le spese e le note della giornata."""

    franchise = models.ForeignKey(Franchise, verbose_name="Punto", on_delete=models.PROTECT, related_name="days")
    cash_total = models.DecimalField(
        "Contanti (€)", max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal("0"))]
    )
    card_total = models.DecimalField(
        "Carta (€)", max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal("0"))]
    )
    expenses = models.DecimalField(
        "Spese (€)", max_digits=10, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))]
    )
    expenses_note = models.CharField("Descrizione delle spese", max_length=200, blank=True)
    notes = models.TextField("Note", max_length=2000, blank=True)

    class Meta:
        ordering = ["-date", "franchise__name"]
        verbose_name = "giornata del franchising"
        verbose_name_plural = "giornate del franchising"
        constraints = [models.UniqueConstraint(fields=["franchise", "date"], name="one_day_per_franchise")]

    def __str__(self):
        return f"{self.franchise} – {self.date:%d/%m/%Y}"

    @property
    def net_total(self):
        """Incasso della giornata (scontrini più Yakuza) meno le spese."""
        return self.day_total - self.expenses

    @property
    def payments_mismatch(self):
        """Contanti e carta scritti entrambi ma la loro somma non torna con gli scontrini."""
        if self.cash_total is None or self.card_total is None:
            return False
        return self.cash_total + self.card_total != self.receipts_total


@receiver(post_delete, sender=Package)
def delete_package_files(sender, instance, **kwargs):
    """Elimina le foto dal disco quando il pacco viene eliminato (anche da queryset/admin).

    Si aspetta il commit della transazione: se l'eliminazione viene annullata
    le foto devono restare dove sono. I pacchi venduti NON passano da qui:
    restano nello storico con la loro foto.
    """
    files = [(f.storage, f.name) for f in (instance.photo, instance.thumb) if f and f.name]

    def _delete():
        for storage, name in files:
            storage.delete(name)

    transaction.on_commit(_delete)


@receiver(post_delete, sender=DayClose)
@receiver(post_delete, sender=FranchiseDay)
def delete_receipt_photo(sender, instance, **kwargs):
    """Elimina la foto dello scontrino dal disco quando la giornata viene eliminata (anche dall'admin)."""
    if not (instance.photo and instance.photo.name):
        return
    storage, name = instance.photo.storage, instance.photo.name
    transaction.on_commit(lambda: storage.delete(name))
