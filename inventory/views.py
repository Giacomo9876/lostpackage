from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Abs
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format, number_format
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView

from . import stats
from .forms import BatchForm, ClosingEntryForm, CloseForm, PackageForm, PriceForm, SaleForm, SearchForm
from .models import Batch, DayClose, Package, ShopSettings


def kg(value):
    return number_format(value, decimal_pos=3)


def euro(value):
    return number_format(value, decimal_pos=2)


def stock_url(request):
    """Magazzino con l'ultima ricerca fatta (dopo una vendita si torna ai risultati)."""
    url = reverse("inventory:list")
    query = request.session.get("list_qs", "")
    return f"{url}?{query}" if query else url


def price_note(shop, package):
    """Come si arriva al prezzo di un pacco, a parole (per il dettaglio e la conferma di vendita)."""
    if package.weight_kg <= Decimal(shop.small_parcel_max_g) / 1000:
        return f"pacco piccolo: prezzo fisso fino a {shop.small_parcel_max_g} g"
    rate = shop.premium_price_per_kg if package.premium else shop.price_per_kg
    kind = "premium, " if package.premium else ""
    return f"{kind}{kg(package.weight_kg)} kg × {euro(rate)} € al kg"


# --- Permessi e centro di lavoro ---------------------------------------------------


class OwnerRequiredMixin:
    """Pagine riservate ai titolari: per lo store manager sono vietate (403)."""

    def dispatch(self, request, *args, **kwargs):
        if not request.scope.is_owner:
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class StoreChoiceMixin:
    """Le pagine che scrivono dati lavorano su un centro.

    Lo store manager ha sempre il suo. Il titolare lo sceglie con ?centro=<id>: senza una
    scelta valida vede l'elenco dei centri da cui partire. Il valore arriva solo dall'indirizzo
    (i moduli inviano al medesimo indirizzo), mai dai campi del modulo.
    """

    choose_title = "Scegli il centro"

    def dispatch(self, request, *args, **kwargs):
        self.store = request.scope.resolve_store(request.GET.get("centro"))
        if self.store is None:
            context = {"stores": request.scope.stores(), "title": self.choose_title}
            return render(request, "inventory/choose_store.html", context)
        return super().dispatch(request, *args, **kwargs)

    def store_url(self, name, *args, **params):
        """Indirizzo di un'altra pagina dello stesso centro (il titolare ha bisogno di ?centro=)."""
        url = reverse(f"inventory:{name}", args=args)
        if self.request.scope.is_owner:
            params["centro"] = self.store.pk
        return f"{url}?{urlencode(params)}" if params else url


# --- Dashboard ---------------------------------------------------------------------


def _dashboard_common(request, period):
    scope = request.scope
    today = timezone.localdate()
    shop = ShopSettings.load()
    packages = scope.packages()
    rows = stats.daily_rows(stats.sold_in(packages, period))
    closes = list(stats.closes_in(scope.closes(), period))
    # Il periodo di confronto (i giorni subito prima) serve per le variazioni in percentuale.
    comparison = stats.compare_with(period)
    before = {}
    if comparison:
        before_rows = stats.daily_rows(stats.sold_in(packages, comparison[0]))
        before = stats.totals_by_store(before_rows, [], today)
    return {
        "scope": scope,
        "today": today,
        "period": period,
        "periods": stats.PERIODS,
        "shop": shop,
        "rows": rows,
        "closes": closes,
        "per_store": stats.totals_by_store(rows, closes, today),
        "before_per_store": before,
        "compare_label": comparison[1] if comparison else None,
        "stock": stats.stock_by_store(packages.in_stock(), shop),
        "closed_today": set(scope.closes().filter(date=today).values_list("store_id", flat=True)),
        "show_private": scope.is_owner,  # Yakuza e differenza scontrini: solo titolari
    }


def dashboard(request):
    """Home: i titolari vedono tutti i centri, lo store manager il proprio."""
    scope = request.scope
    if not scope.is_owner:
        return _store_dashboard(request, scope.store)

    period = stats.get_period(request.GET.get("periodo"))
    data = _dashboard_common(request, period)
    cards = []
    for store in scope.stores():
        totals = data["per_store"].get(store.pk) or stats.Totals()
        before = data["before_per_store"].get(store.pk) or stats.Totals()
        cards.append(
            {
                "store": store,
                "totals": totals,
                "change": stats.change_pct(totals.revenue, before.revenue) if data["compare_label"] else None,
                "stock": data["stock"].get(store.pk) or {"count": 0, "weight": Decimal("0"), "value": stats.ZERO},
                "closed_today": store.pk in data["closed_today"],
            }
        )
    total = stats.sum_totals(data["per_store"])
    total_before = stats.sum_totals(data["before_per_store"])
    context = {
        **data,
        "cards": cards,
        "total": total,
        "insights": stats.insights(data["rows"], total, period, data["today"]),
        "revenue_change": stats.change_pct(total.revenue, total_before.revenue),
        "count_change": stats.change_pct(total.count, total_before.count),
        "bars": stats.chart_days(data["rows"], period) if period.key != "oggi" else [],
        "stock_total": {
            "count": sum(item["stock"]["count"] for item in cards),
            "value": sum((item["stock"]["value"] for item in cards), stats.ZERO),
        },
    }
    return render(request, "inventory/dashboard.html", context)


def store_dashboard(request, pk):
    store = get_object_or_404(request.scope.stores(), pk=pk)
    return _store_dashboard(request, store)


def _store_dashboard(request, store):
    scope = request.scope
    default = "7g" if scope.is_owner else "oggi"
    period = stats.get_period(request.GET.get("periodo"), default=default)
    data = _dashboard_common(request, period)
    rows = [row for row in data["rows"] if row["store_id"] == store.pk]
    closes = [close for close in data["closes"] if close.store_id == store.pk]
    today_close = scope.closes().filter(store=store, date=data["today"]).first()
    today_count = scope.packages().filter(store=store).sold().filter(
        sold_at__gte=stats.start_of_day(data["today"])
    ).count()
    totals = data["per_store"].get(store.pk) or stats.Totals()
    before = data["before_per_store"].get(store.pk) or stats.Totals()
    context = {
        **data,
        "store": store,
        "totals": totals,
        "insights": stats.insights(rows, totals, period, data["today"]),
        "revenue_change": stats.change_pct(totals.revenue, before.revenue),
        "count_change": stats.change_pct(totals.count, before.count),
        "stock": data["stock"].get(store.pk) or {"count": 0, "weight": Decimal("0"), "value": stats.ZERO},
        "bars": stats.chart_days(rows, period) if period.key != "oggi" else [],
        "days": stats.day_list(rows, closes),
        "today_close": today_close,
        "today_count": today_count,
        "centro_param": f"?centro={store.pk}" if scope.is_owner else "",
    }
    return render(request, "inventory/store_dashboard.html", context)


# --- Chiusura di giornata ----------------------------------------------------------


class ClosingView(StoreChoiceMixin, TemplateView):
    """Fine giornata: i pacchi venduti (peso, premium, scontrino) e il totale degli scontrini in cassa."""

    template_name = "inventory/closing.html"
    choose_title = "Chiusura di giornata: scegli il centro"

    def get_day(self):
        today = timezone.localdate()
        try:
            day = date.fromisoformat(self.request.GET.get("data", ""))
        except ValueError:
            return today
        if day > today or not self.request.scope.can_edit_day(day):
            return today
        return day

    def day_packages(self, day):
        period = stats.Period(key="giorno", label="", start=day, end=day)
        return stats.sold_in(self.request.scope.packages().filter(store=self.store), period)

    def sale_time(self, day):
        if day == timezone.localdate():
            return timezone.now()
        return stats.start_of_day(day) + timedelta(hours=12)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        day = self.get_day()
        today = timezone.localdate()
        day_qs = self.day_packages(day)
        entries = list(day_qs.select_related("batch").order_by("-sold_at", "-id"))
        for entry in entries:
            # Si elimina da qui solo ciò che è stato inserito a fine giornata (senza foto né lotto);
            # gli altri pacchi si aprono dal dettaglio e si rimettono in magazzino.
            entry.deletable = not entry.has_photo and not entry.batch_id
        yakuza = Q(payment=Package.Payment.YAKUZA)
        totals = day_qs.aggregate(
            revenue=Sum("sold_price"),
            yakuza=Sum("sold_price", filter=yakuza),
            yakuza_count=Count("id", filter=yakuza),
        )
        close = self.request.scope.closes().filter(store=self.store, date=day).first()
        context.update(
            store=self.store,
            day=day,
            is_today=day == today,
            earliest=self.request.scope.earliest_editable_day(),
            yesterday=today - timedelta(days=1),
            today_iso=today.isoformat(),
            entries=entries,
            count=len(entries),
            revenue=totals["revenue"] or stats.ZERO,
            yakuza=totals["yakuza"] or stats.ZERO,
            yakuza_count=totals["yakuza_count"],
            close=close,
            rules=self.price_rules(ShopSettings.load()),
            entry_form=kwargs.get("entry_form") or ClosingEntryForm(initial={"payment": self.last_payment()}),
            close_form=kwargs.get("close_form") or CloseForm(
                initial={"receipts_total": close.receipts_total} if close else None
            ),
            show_private=self.request.scope.is_owner,
        )
        return context

    def last_payment(self):
        """Il pagamento dell'ultimo pacco inserito: di solito si fanno più pacchi di fila con lo stesso."""
        value = self.request.session.get("last_payment")
        return value if value in Package.Payment.values else Package.Payment.CASH

    @staticmethod
    def price_rules(shop):
        """Regole di prezzo in centesimi per l'anteprima del prezzo nel browser (il calcolo vero è sul server)."""

        def cents(value):
            return int((Decimal(value) * 100).quantize(Decimal(1)))

        return {
            "small_max_g": shop.small_parcel_max_g,
            "small_cents": cents(shop.small_parcel_price),
            "rate_cents": cents(shop.price_per_kg),
            "premium_cents": cents(shop.premium_price_per_kg),
        }

    def page_url(self, day):
        params = {"data": day.isoformat()} if day != timezone.localdate() else {}
        return self.store_url("closing", **params)

    def post(self, request, *args, **kwargs):
        day = self.get_day()
        action = request.POST.get("azione")
        if action == "pacco":
            form = ClosingEntryForm(request.POST)
            if form.is_valid():
                return self.add_entry(form, day)
            return self.render_to_response(self.get_context_data(entry_form=form))
        if action == "chiusura":
            form = CloseForm(request.POST)
            if form.is_valid():
                return self.close_day(form, day)
            return self.render_to_response(self.get_context_data(close_form=form))
        return HttpResponseRedirect(self.page_url(day))

    def add_entry(self, form, day):
        data = form.cleaned_data
        # Il prezzo da listino lo calcola sempre il server; chi vende può scriverne un altro (sconto).
        list_price = ShopSettings.load().price_for(data["weight_kg"], data["premium"])
        price = list_price if data["price"] is None else data["price"]
        package = Package.objects.create(
            store=self.store,
            weight_kg=data["weight_kg"],
            premium=data["premium"],
            payment=data["payment"],
            sold_at=self.sale_time(day),
            sold_price=price,
            list_price=list_price,
            sold_by=self.request.user,
            created_by=self.request.user,
        )
        self.request.session["last_payment"] = package.payment
        text = f"Pacco #{package.pk}: {kg(package.weight_kg)} kg, {euro(price)} €"
        if price != list_price:
            text += f" (listino {euro(list_price)} €)"
        messages.success(self.request, text + ".")
        return HttpResponseRedirect(self.page_url(day))

    def close_day(self, form, day):
        total = form.cleaned_data["receipts_total"]
        values = {"receipts_total": total, "closed_by": self.request.user}
        try:
            with transaction.atomic():
                DayClose.objects.update_or_create(store=self.store, date=day, defaults=values)
        except IntegrityError:
            # Due invii quasi insieme: la riga esiste ormai, basta aggiornarla.
            DayClose.objects.filter(store=self.store, date=day).update(**values)
        label = date_format(day, "j F")
        messages.success(self.request, f"Giornata del {label} chiusa: scontrini battuti {euro(total)} €.")
        return HttpResponseRedirect(self.page_url(day))


@require_POST
def closing_entry_delete(request, pk):
    """Toglie un pacco inserito per errore a fine giornata (solo se senza foto né lotto)."""
    package = get_object_or_404(request.scope.packages().sold(), pk=pk)
    if package.has_photo or package.batch_id or not request.scope.can_edit_sale(package):
        raise PermissionDenied
    day = timezone.localdate(package.sold_at)
    store_id, number = package.store_id, package.pk
    package.delete()
    messages.success(request, f"Pacco #{number} eliminato.")
    params = {}
    if request.scope.is_owner:
        params["centro"] = store_id
    if day != timezone.localdate():
        params["data"] = day.isoformat()
    url = reverse("inventory:closing")
    return redirect(f"{url}?{urlencode(params)}" if params else url)


# --- Magazzino e storico -----------------------------------------------------------


class PackageListView(ListView):
    """Magazzino (pacchi non venduti) e storico (pacchi venduti), con ricerca per peso e lotto."""

    model = Package
    paginate_by = 24
    template_name = "inventory/package_list.html"
    context_object_name = "packages"
    history = False

    def get(self, request, *args, **kwargs):
        request.session["history_qs" if self.history else "list_qs"] = request.GET.urlencode()
        self.form = SearchForm(request.GET or None, scope=request.scope)
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        packages = self.request.scope.packages().select_related("batch", "store")
        if self.history:
            qs = packages.sold().order_by("-sold_at", "-id")
        else:
            qs = packages.in_stock()
        self.target = self.low = self.high = None

        if not self.form.is_bound:
            return qs
        if not self.form.is_valid():
            return qs.none()

        data = self.form.cleaned_data
        if data.get("centro"):
            qs = qs.filter(store=data["centro"])
        if data["batch"]:
            qs = qs.filter(batch=data["batch"])
        if data["peso"] is not None:
            tolerance = Decimal(data["tolleranza"]) / 1000
            self.target = data["peso"]
            self.low = max(self.target - tolerance, Decimal("0"))
            self.high = self.target + tolerance
            distance = Abs(F("weight_kg") - Value(self.target, output_field=DecimalField()))
            qs = (
                qs.filter(weight_kg__gte=self.low, weight_kg__lte=self.high)
                # A parità di distanza (es. tre pacchi dello stesso peso) l'ordine è stabile.
                .annotate(distance=distance)
                .order_by("distance", "id")
            )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        shop = ShopSettings.load()
        searching = self.target is not None
        for package in context["packages"]:
            if searching:
                package.delta_g = int((package.weight_kg - self.target) * 1000)
            if not self.history:
                package.price = package.price_with(shop)

        if self.history:
            totals = self.object_list.aggregate(weight=Sum("weight_kg"), revenue=Sum("sold_price"))
            total_weight = totals["weight"] or Decimal("0")
            revenue = totals["revenue"] or stats.ZERO
            stock_value = None
        else:
            stock = stats.stock_by_store(self.object_list, shop).values()
            total_weight = sum((item["weight"] for item in stock), Decimal("0"))
            stock_value = sum((item["value"] for item in stock), stats.ZERO)
            revenue = None
        context.update(
            form=self.form,
            history=self.history,
            searching=searching,
            filtered=self.form.is_bound,
            low=self.low,
            high=self.high,
            shop=shop,
            total_count=context["paginator"].count if context["paginator"] else 0,
            total_weight=total_weight,
            stock_value=stock_value,
            revenue=revenue,
        )
        return context


class PackageCreateView(StoreChoiceMixin, CreateView):
    model = Package
    form_class = PackageForm
    template_name = "inventory/package_form.html"
    choose_title = "Registra un pacco: scegli il centro"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["store"] = self.store
        return kwargs

    def get_batch(self):
        """Lotto in lavorazione, se c'è: da ?lotto=3 oppure l'ultimo usato in questo centro."""
        if not hasattr(self, "_batch"):
            batch_id = self.request.GET.get("lotto") or self.request.session.get("last_batch_id")
            valid = str(batch_id).isdigit()
            self._batch = Batch.objects.filter(store=self.store, pk=batch_id).first() if valid else None
        return self._batch

    def get_initial(self):
        batch = self.get_batch()
        return {"batch": batch.pk if batch else None}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["store"] = self.store
        batch = self.get_batch()
        if batch:
            registered = batch.packages.count()
            context.update(
                batch=batch,
                registered=registered,
                expected=batch.expected_count,
                next_number=registered + 1,
                complete=bool(batch.expected_count) and registered >= batch.expected_count,
            )
        return context

    def form_valid(self, form):
        form.instance.store = self.store
        form.instance.created_by = self.request.user
        self.object = form.save()
        messages.success(self.request, f"Pacco #{self.object.pk} salvato: {kg(self.object.weight_kg)} kg.")
        self.batch_complete = False
        batch = self.object.batch
        if batch is None:
            # Senza lotto non si riproporre quello di prima.
            self.request.session.pop("last_batch_id", None)
            return HttpResponseRedirect(self.get_success_url())

        self.request.session["last_batch_id"] = batch.pk
        registered = batch.packages.count()
        expected = batch.expected_count
        if expected and registered > expected:
            # Il totale era una previsione: se arrivano più pacchi del previsto si aggiorna, senza bloccare nulla.
            batch.expected_count = registered
            batch.save(update_fields=["expected_count"])
            messages.info(
                self.request, f"Il lotto «{batch.name}» ora ha {registered} pacchi (ne erano previsti {expected})."
            )
        elif expected and registered == expected:
            self.batch_complete = True
            messages.success(
                self.request, f"Lotto «{batch.name}» completo: {registered} di {expected} pacchi registrati."
            )
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        if "again" in self.request.POST:
            if self.batch_complete:
                return self.store_url("batch_list")
            params = {"lotto": self.object.batch_id} if self.object.batch_id else {}
            return self.store_url("create", **params)
        return reverse("inventory:detail", args=[self.object.pk])


class PackageScopedMixin:
    model = Package

    def get_queryset(self):
        return self.request.scope.packages().select_related("batch", "store", "created_by", "sold_by")


class PackageDetailView(PackageScopedMixin, DetailView):
    template_name = "inventory/package_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        shop = ShopSettings.load()
        scope = self.request.scope
        context.update(
            shop=shop,
            price=self.object.price_with(shop),
            price_note=price_note(shop, self.object),
            can_edit=not self.object.is_sold or scope.can_edit_sale(self.object),
            can_fix_price=scope.is_owner,
        )
        return context


class PackageUpdateView(PackageScopedMixin, UpdateView):
    form_class = PackageForm
    template_name = "inventory/package_form.html"

    def get_object(self, queryset=None):
        # I pacchi di altri centri non arrivano nemmeno qui (404). Un manager può modificare
        # un pacco venduto solo se la vendita è di oggi o di ieri.
        package = super().get_object(queryset)
        if package.is_sold and not self.request.scope.can_edit_sale(package):
            raise PermissionDenied
        return package

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["store"] = self.object.store
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["store"] = self.object.store
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Pacco #{self.object.pk} aggiornato.")
        return response

    def get_success_url(self):
        return reverse("inventory:detail", args=[self.object.pk])


class PackageSellView(PackageScopedMixin, UpdateView):
    """Vendita: il pacco passa allo storico. Sul pacco già venduto serve al titolare per correggere il prezzo."""

    form_class = SaleForm
    template_name = "inventory/package_sell.html"

    def get_object(self, queryset=None):
        package = super().get_object(queryset)
        if package.is_sold and not self.request.scope.is_owner:
            raise PermissionDenied
        return package

    def get_initial(self):
        if self.object.is_sold:
            return {"sold_price": self.object.sold_price}
        return {"sold_price": self.object.price_with(ShopSettings.load())}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        shop = ShopSettings.load()
        context.update(
            shop=shop,
            price=self.object.price_with(shop),
            price_note=price_note(shop, self.object),
        )
        return context

    def form_valid(self, form):
        package = form.instance
        self.first_sale = not package.is_sold  # serve a get_success_url, che super() chiama subito
        if self.first_sale:
            package.sold_at = timezone.now()
            package.sold_by = self.request.user
            # Il listino lo calcola il server; il prezzo scritto nel modulo può essere diverso (sconto).
            package.list_price = package.price_with(ShopSettings.load())
        response = super().form_valid(form)
        if self.first_sale:
            text = f"Pacco #{package.pk} venduto a {euro(package.sold_price)} €"
            if package.price_adjustment:
                text += f" (listino {euro(package.list_price)} €)"
            messages.success(self.request, text + ": ora è nello storico.")
        else:
            messages.success(self.request, f"Pacco #{package.pk} corretto: {euro(package.sold_price)} €.")
        return response

    def get_success_url(self):
        if self.first_sale:
            return stock_url(self.request)
        return reverse("inventory:detail", args=[self.object.pk])


@require_POST
def package_restore(request, pk):
    """Annulla una vendita: il pacco torna in magazzino."""
    package = get_object_or_404(request.scope.packages().sold(), pk=pk)
    if not request.scope.can_edit_sale(package):
        raise PermissionDenied
    package.sold_at = package.sold_price = package.list_price = package.sold_by = None
    package.payment = Package.Payment.CASH
    package.save(update_fields=["sold_at", "sold_price", "list_price", "sold_by", "payment", "updated_at"])
    messages.success(request, f"Pacco #{package.pk} rimesso in magazzino.")
    return redirect("inventory:detail", pk=package.pk)


class PackageDeleteView(PackageScopedMixin, DeleteView):
    """Eliminazione definitiva, per un pacco registrato per errore."""

    template_name = "inventory/package_confirm_delete.html"

    def get_object(self, queryset=None):
        package = super().get_object(queryset)
        if package.is_sold and not self.request.scope.can_edit_sale(package):
            raise PermissionDenied
        return package

    def form_valid(self, form):
        pk = self.object.pk
        self.was_sold = self.object.is_sold
        response = super().form_valid(form)
        messages.success(self.request, f"Pacco #{pk} eliminato.")
        return response

    def get_success_url(self):
        if self.was_sold:
            return reverse("inventory:history")
        return stock_url(self.request)


# --- Lotti -------------------------------------------------------------------------


class BatchListView(ListView):
    template_name = "inventory/batch_list.html"
    context_object_name = "batches"

    def get_queryset(self):
        return self.request.scope.batches().select_related("store").annotate(
            registered=Count("packages"),
            in_stock=Count("packages", filter=Q(packages__sold_at__isnull=True)),
            sold=Count("packages", filter=Q(packages__sold_at__isnull=False)),
        )


class BatchCreateView(StoreChoiceMixin, CreateView):
    model = Batch
    form_class = BatchForm
    template_name = "inventory/batch_form.html"
    choose_title = "Nuovo lotto: scegli il centro"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["store"] = self.store
        return kwargs

    def get_initial(self):
        batches = Batch.objects.filter(store=self.store)
        number = batches.count() + 1
        while batches.filter(name__iexact=f"Lotto {number}").exists():
            number += 1
        return {"name": f"Lotto {number}"}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["store"] = self.store
        return context

    def form_valid(self, form):
        form.instance.store = self.store
        response = super().form_valid(form)
        if self.object.expected_count:
            text = f"Lotto «{self.object.name}» creato: ora registra i suoi {self.object.expected_count} pacchi."
        else:
            text = f"Lotto «{self.object.name}» creato: registra i pacchi, il totale lo potrai indicare più avanti."
        messages.success(self.request, text)
        return response

    def get_success_url(self):
        return self.store_url("create", lotto=self.object.pk)


class BatchUpdateView(UpdateView):
    model = Batch
    form_class = BatchForm
    template_name = "inventory/batch_form.html"

    def get_queryset(self):
        return self.request.scope.batches().select_related("store")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["store"] = self.object.store
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["from_register"] = self.request.GET.get("da") == "registra"
        context["store"] = self.object.store
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Lotto «{self.object.name}» aggiornato.")
        return response

    def get_success_url(self):
        # Arrivando dalla registrazione si torna lì, a continuare con lo stesso lotto.
        if self.request.GET.get("da") == "registra":
            params = {"lotto": self.object.pk}
            if self.request.scope.is_owner:
                params["centro"] = self.object.store_id
            return f"{reverse('inventory:create')}?{urlencode(params)}"
        return reverse("inventory:batch_list")


# --- Prezzi e foto -----------------------------------------------------------------


class PriceUpdateView(OwnerRequiredMixin, UpdateView):
    """Regole di prezzo: valgono per i pacchi in magazzino e per le nuove vendite.

    Le vendite passate tengono il loro prezzo. Solo i titolari le cambiano.
    """

    model = ShopSettings
    form_class = PriceForm
    template_name = "inventory/price_form.html"

    def get_object(self, queryset=None):
        return ShopSettings.load()

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Prezzi aggiornati.")
        return response

    def get_success_url(self):
        return reverse("inventory:dashboard")


@require_GET
def package_image(request, pk, kind):
    """Serve foto e miniatura solo a chi può vedere quel pacco (il middleware blocca gli anonimi)."""
    package = get_object_or_404(request.scope.packages().only("photo", "thumb"), pk=pk)
    field = {"foto": package.photo, "miniatura": package.thumb}.get(kind)
    if not field:
        raise Http404
    try:
        handle = field.open("rb")
    except FileNotFoundError:
        raise Http404
    response = FileResponse(handle, content_type="image/jpeg")
    # Il nome del file non cambia mai per la stessa foto; la query ?v=... nei template cambia se si sostituisce.
    response["Cache-Control"] = "private, max-age=86400"
    return response
