from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Count, Q

from .models import Batch, DayClose, Profile, Store

User = get_user_model()


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("name", "city")
    search_fields = ("name", "city")


class ProfileInline(admin.StackedInline):
    """Ruolo e centro dell'utente: senza profilo (e senza essere superuser) non si entra nell'app."""

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
    list_select_related = ("profile__store",)

    @admin.display(description="Ruolo")
    def ruolo(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.get_role_display().split(" (")[0] if profile else "—"

    @admin.display(description="Centro")
    def centro(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.store if profile and profile.store else "—"


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
    list_display = ("date", "store", "receipts_total", "closed_by", "updated_at")
    list_filter = ("store",)
    date_hierarchy = "date"
