"""Sezione Franchising: ogni punto affiliato inserisce le sue giornate e vede solo i suoi numeri.

Chi è in franchising entra solo qui (lo garantisce `StoreAccessMiddleware`) e vede solo il proprio punto
(lo garantisce `Scope`). I titolari vedono tutti i punti, a parte rispetto ai centri: questi numeri non
entrano mai nella dashboard né nelle medie dei centri. Gli store manager qui non entrano.
"""
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Max, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import TemplateView

from . import averages, stats
from .forms import FranchiseDayForm
from .views import DayEntryMixin, DayMixin, serve_receipt_photo


def _franchise_url(request, franchise, name, **params):
    """Indirizzo di una pagina del punto: il titolare ha bisogno di ?punto=, chi è in franchising no."""
    url = reverse(f"inventory:{name}")
    if request.scope.is_owner and franchise is not None:
        params["punto"] = franchise.pk
    return f"{url}?{urlencode(params)}" if params else url


def _require_franchising(request):
    if not request.scope.sees_franchising:
        raise PermissionDenied


class FranchiseView(TemplateView):
    """Il punto: le tre medie, i totali del periodo e le giornate. Il titolare senza ?punto= vede tutti i punti."""

    template_name = "inventory/franchise.html"

    def get(self, request, *args, **kwargs):
        _require_franchising(request)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        scope = request.scope
        today = timezone.localdate()
        period = averages.get_period(request.GET.get("periodo"), today=today)
        franchise = scope.resolve_franchise(request.GET.get("punto"))
        days = scope.franchise_days()
        context.update(today=today, period=period, periods=averages.PERIODS, franchise=franchise, show_private=True)

        if franchise is None:  # solo il titolare: tutti i punti a confronto
            rows = averages.closing_rows(days, period, key="franchise_id")
            computed = averages.compute_by_store(rows, period, today)
            last = dict(days.values("franchise_id").annotate(last=Max("date")).values_list("franchise_id", "last"))
            context["overview"] = True
            context["cards"] = [
                (item, computed.get(item.pk) or averages.Averages(), last.get(item.pk)) for item in scope.franchises()
            ]
            return context

        days = days.filter(franchise=franchise)
        average = averages.compute(averages.closing_rows(days, period, key="franchise_id"), period, today)
        in_period = stats.closes_in(days, period)
        sums = in_period.aggregate(
            receipts=Sum("receipts_total"),
            yakuza=Sum("yakuza_cash"),
            expenses=Sum("expenses"),
            cash=Sum("cash_total"),
            card=Sum("card_total"),
            cash_days=Count("cash_total"),
            card_days=Count("card_total"),
            days=Count("id"),
        )
        income = (sums["receipts"] or stats.ZERO) + (sums["yakuza"] or stats.ZERO)
        expenses = sums["expenses"] or stats.ZERO
        listed = list(in_period.select_related("closed_by").order_by("-date")[: averages.LIST_LIMIT + 1])
        for day in listed[: averages.LIST_LIMIT]:
            day.edit_url = _franchise_url(request, franchise, "franchise_day", data=day.date.isoformat())
        context.update(
            average=average,
            chart=averages.chart_rows(average),
            totals={**sums, "income": income, "expenses": expenses, "net": income - expenses},
            days=listed[: averages.LIST_LIMIT],
            more=len(listed) > averages.LIST_LIMIT,
            list_limit=averages.LIST_LIMIT,
            add_url=_franchise_url(request, franchise, "franchise_day", nuova=1),
            all_url=reverse("inventory:franchise") if scope.is_owner else None,
        )
        return context


class FranchiseChoiceMixin:
    """Il punto su cui si lavora: chi è in franchising ha il suo; il titolare lo sceglie con ?punto=<id>."""

    choose_title = "Franchising: scegli il punto"

    def dispatch(self, request, *args, **kwargs):
        _require_franchising(request)
        self.franchise = request.scope.resolve_franchise(request.GET.get("punto"))
        if self.franchise is None:
            context = {"franchises": request.scope.franchises(), "title": self.choose_title}
            return render(request, "inventory/choose_franchise.html", context)
        return super().dispatch(request, *args, **kwargs)


class FranchiseDayView(DayEntryMixin, DayMixin, FranchiseChoiceMixin, TemplateView):
    """La giornata di un punto: scontrini (anche dalla foto), Yakuza, contanti e carta, spese, note."""

    form_class = FranchiseDayForm
    list_url_name = "franchise"
    day_url_name = "franchise_day"
    photo_url_name = "franchise_photo"
    delete_url_name = "franchise_delete"
    page_title = "Giornata del franchising"

    @property
    def place(self):
        return self.franchise

    def current(self, day):
        return self.request.scope.franchise_days().filter(franchise=self.franchise, date=day).first()

    def page_url(self, name, **params):
        return _franchise_url(self.request, self.franchise, name, **params)

    def may_pick_day(self):
        return True  # il titolare e chi è in franchising inseriscono anche le giornate passate

    def change_place_url(self):
        if not self.request.scope.is_owner:
            return None
        return reverse("inventory:franchise_day") + ("?nuova=1" if self.adding(self.get_day()) else "")

    def initial_for(self, close):
        return {
            **super().initial_for(close),
            "cash_total": close.cash_total,
            "card_total": close.card_total,
            "expenses": close.expenses or None,
            "expenses_note": close.expenses_note,
            "notes": close.notes,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(franchise=self.franchise, franchise_fields=True)
        return context


@require_GET
def franchise_photo(request, pk):
    """Foto dello scontrino di una giornata del franchising: solo a chi vede quel punto."""
    _require_franchising(request)
    return serve_receipt_photo(get_object_or_404(request.scope.franchise_days().only("photo"), pk=pk))


@require_POST
def franchise_delete(request, pk):
    """Toglie una giornata inserita per errore."""
    _require_franchising(request)
    day = get_object_or_404(request.scope.franchise_days().select_related("franchise"), pk=pk)
    if not request.scope.can_edit_day(day.date):
        raise PermissionDenied
    franchise, date = day.franchise, day.date
    day.delete()
    messages.success(request, f"Giornata del {date_format(date, 'j F Y').lower()} eliminata.")
    return redirect(_franchise_url(request, franchise, "franchise"))
