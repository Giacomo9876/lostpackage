from django.contrib import admin
from django.db.models import Count, Q

from .models import Batch


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    """I pacchi si gestiscono solo dall'app: la foto deve passare dall'elaborazione in forms.py."""

    list_display = ("name", "expected_count", "created_at", "in_stock")
    search_fields = ("name",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _in_stock=Count("packages", filter=Q(packages__sold_at__isnull=True))
        )

    @admin.display(description="Pacchi in magazzino", ordering="_in_stock")
    def in_stock(self, obj):
        return obj._in_stock
