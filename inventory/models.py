import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

CENT = Decimal("0.01")


def photo_upload_path(instance, filename):
    # Il nome originale del file non viene mai usato: solo un UUID casuale.
    return f"packages/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.jpg"


def thumb_upload_path(instance, filename):
    return f"packages/{timezone.now():%Y/%m}/thumbs/{uuid.uuid4().hex}.jpg"


class ShopSettings(models.Model):
    """Impostazioni del negozio. Esiste una sola riga: oggi contiene il prezzo al chilo."""

    price_per_kg = models.DecimalField(
        "Prezzo al kg (€)",
        max_digits=7,
        decimal_places=2,
        default=Decimal("25.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "impostazioni"
        verbose_name_plural = "impostazioni"

    @classmethod
    def load(cls):
        settings_row, _ = cls.objects.get_or_create(pk=1)
        return settings_row


class Batch(models.Model):
    """Un lotto di pacchi arrivati insieme, con il numero di pacchi che ci si aspetta."""

    name = models.CharField("Nome", max_length=80, unique=True)
    # 0 = non indicato (lotti creati prima che esistesse questo campo).
    expected_count = models.PositiveIntegerField("Numero di pacchi", default=0)
    created_at = models.DateTimeField("Creato il", auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "lotto"
        verbose_name_plural = "lotti"

    def __str__(self):
        return self.name


class PackageQuerySet(models.QuerySet):
    def in_stock(self):
        return self.filter(sold_at__isnull=True)

    def sold(self):
        return self.filter(sold_at__isnull=False)


class Package(models.Model):
    """Un pacco. Finché `sold_at` è vuoto è in magazzino; venduto finisce nello storico."""

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
    photo = models.ImageField("Foto", upload_to=photo_upload_path, max_length=255)
    thumb = models.ImageField(upload_to=thumb_upload_path, max_length=255, editable=False)
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

    objects = PackageQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["weight_kg"]), models.Index(fields=["sold_at"])]
        verbose_name = "pacco"
        verbose_name_plural = "pacchi"

    def __str__(self):
        return f"Pacco #{self.pk} ({self.weight_kg} kg)"

    @property
    def is_sold(self):
        return self.sold_at is not None

    def price_at(self, price_per_kg):
        """Prezzo calcolato dal peso, arrotondato al centesimo."""
        return (self.weight_kg * price_per_kg).quantize(CENT, rounding=ROUND_HALF_UP)


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
