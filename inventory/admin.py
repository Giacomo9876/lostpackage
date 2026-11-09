from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Count, Q

from .models import Batch, DayClose, Franchise, FranchiseDay, Profile, Store, SupplyItem, SupplyStatus

User = get_user_model()


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("name", "city")
    search_fields = ("name", "city")


@admin.register(Franchise)
class FranchiseAdmin(admin.ModelAdmin):
    """Un punto in franchising. Per farci entrare qualcuno: utente con ruolo «Franchising» e questo punto."""

    list_display = ("name", "city")
    search_fields = ("name", "city")


class ProfileInline(admin.StackedInline):
    """Ruolo e centro (o punto in franchising) dell'utente: senza profilo (e senza essere superuser) non si entra."""

    model = Profile
    can_delete = False
    # Il modulo del ruolo c'è sempre e non si può lasciare com'è: senza un ruolo l'utente non
    # entrerebbe nell'app, e un modulo extra non modificato verrebbe saltato in silenzio.
    min_num = 1
    max_num = 1
    extra = 0
    verbose_name = verbose_name_plural = "Ruolo nel gestionale"


admin.site.unregister(User)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [ProfileInline]
    list_display = ("username", "first_name", "last_name", "ruolo", "centro", "is_active")
    list_select_related = ("profile__store", "profile__franchise")

    @admin.display(description="Ruolo")
    def ruolo(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.get_role_display().split(" (")[0] if profile else "—"

    @admin.display(description="Centro o punto")
    def centro(self, obj):
        profile = getattr(obj, "profile", None)
        if profile and profile.store:
            return profile.store
        if profile and profile.franchise:
            return f"{profile.franchise} (franchising)"
        return "—"


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    """I pacchi si gestiscono solo dall'app: la foto deve passare dall'elaborazione in forms.py."""

    list_display = ("name", "store", "expected_count", "created_at", "in_stock")
    list_filter = ("store",)
    search_fields = ("name",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _in_stock=Count("packages", filter=Q(packages__sold_at__isnull=True))
        )

    @admin.display(description="Pacchi in magazzino", ordering="_in_stock")
    def in_stock(self, obj):
        return obj._in_stock


@admin.register(DayClose)
class DayCloseAdmin(admin.ModelAdmin):
    """Le foto degli scontrini si caricano solo dall'app: devono passare dall'elaborazione in images.py."""

    list_display = ("date", "store", "receipts_total", "cash_total", "card_total", "cash_excess", "yakuza_cash", "closed_by", "updated_at")
    list_filter = ("store",)
    date_hierarchy = "date"
    exclude = ("photo", "detected_total")
    readonly_fields = ("yakuza_cash",)  # calcolato: eccesso cassa meno contanti dello scontrino


@admin.register(FranchiseDay)
class FranchiseDayAdmin(admin.ModelAdmin):
    """Le giornate dei punti in franchising (la foto si carica solo dall'app, come per le chiusure)."""

    list_display = ("date", "franchise", "receipts_total", "cash_total", "card_total", "cash_excess", "yakuza_cash", "expenses", "closed_by")
    list_filter = ("franchise",)
    date_hierarchy = "date"
    exclude = ("photo", "detected_total")
    readonly_fields = ("yakuza_cash",)  # calcolato: eccesso cassa meno contanti dello scontrino


@admin.register(SupplyItem)
class SupplyItemAdmin(admin.ModelAdmin):
    """Le voci della sezione Risorse. Per toglierne una dall'app basta togliere la spunta «Attiva»."""

    list_display = ("name", "has_quantity", "unit", "order", "active")
    list_editable = ("order", "active")
    search_fields = ("name",)


@admin.register(SupplyStatus)
class SupplyStatusAdmin(admin.ModelAdmin):
    """Lo stato delle risorse lo aggiornano gli store manager e i franchising dall'app: qui si guarda soltanto."""

    list_display = ("item", "store", "franchise", "level", "quantity", "note", "updated_by", "updated_at")
    list_filter = ("level", "item", "store", "franchise")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
