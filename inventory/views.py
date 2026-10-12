from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Abs
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import number_format
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import BatchForm, PackageForm, PriceForm, SaleForm, SearchForm
from .models import CENT, Batch, Package, ShopSettings


def kg(value):
    return number_format(value, decimal_pos=3)


def euro(value):
    return number_format(value, decimal_pos=2)


def stock_url(request):
    """Magazzino con l'ultima ricerca fatta (dopo una vendita si torna ai risultati)."""
    url = reverse("inventory:list")
    query = request.session.get("list_qs", "")
    return f"{url}?{query}" if query else url


class PackageListView(ListView):
    """Magazzino (pacchi non venduti) e storico (pacchi venduti), con ricerca per peso e lotto."""

    model = Package
    paginate_by = 24
    template_name = "inventory/package_list.html"
    context_object_name = "packages"
    history = False

    def get(self, request, *args, **kwargs):
        request.session["history_qs" if self.history else "list_qs"] = request.GET.urlencode()
        self.form = SearchForm(request.GET or None)
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        if self.history:
            qs = Package.objects.sold().select_related("batch").order_by("-sold_at", "-id")
        else:
            qs = Package.objects.in_stock().select_related("batch")
        self.target = self.low = self.high = None

        if not self.form.is_bound:
            return qs
        if not self.form.is_valid():
            return qs.none()

        data = self.form.cleaned_data
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
        price_per_kg = ShopSettings.load().price_per_kg
        searching = self.target is not None
        for package in context["packages"]:
            if searching:
                package.delta_g = int((package.weight_kg - self.target) * 1000)
            if not self.history:
                package.price = package.price_at(price_per_kg)

        totals = self.object_list.aggregate(weight=Sum("weight_kg"), revenue=Sum("sold_price"))
        total_weight = totals["weight"] or Decimal("0")
        context.update(
            form=self.form,
            history=self.history,
            searching=searching,
            filtered=self.form.is_bound,
            low=self.low,
            high=self.high,
            price_per_kg=price_per_kg,
            total_count=context["paginator"].count if context["paginator"] else 0,
            total_weight=total_weight,
            stock_value=(total_weight * price_per_kg).quantize(CENT),
            revenue=totals["revenue"] or Decimal("0"),
        )
        return context


class PackageCreateView(CreateView):
    model = Package
    form_class = PackageForm
    template_name = "inventory/package_form.html"

    def get(self, request, *args, **kwargs):
        if not Batch.objects.exists():
            messages.info(request, "Per registrare un pacco serve prima un lotto: crealo qui.")
            return redirect("inventory:batch_create")
        return super().get(request, *args, **kwargs)

    def get_batch(self):
        """Lotto in lavorazione: da ?lotto=3 oppure l'ultimo usato."""
        if not hasattr(self, "_batch"):
            batch_id = self.request.GET.get("lotto") or self.request.session.get("last_batch_id")
            valid = str(batch_id).isdigit()
            self._batch = Batch.objects.filter(pk=batch_id).first() if valid else None
        return self._batch

    def get_initial(self):
        batch = self.get_batch()
        return {"batch": batch.pk if batch else None}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
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
        form.instance.created_by = self.request.user
        self.object = form.save()
        batch = self.object.batch
        self.request.session["last_batch_id"] = batch.pk
        messages.success(self.request, f"Pacco #{self.object.pk} salvato: {kg(self.object.weight_kg)} kg.")
        registered = batch.packages.count()
        expected = batch.expected_count
        self.batch_complete = False
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
                return reverse("inventory:batch_list")
            return f"{reverse('inventory:create')}?lotto={self.object.batch_id}"
        return reverse("inventory:detail", args=[self.object.pk])


class PackageDetailView(DetailView):
    model = Package
    template_name = "inventory/package_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        price_per_kg = ShopSettings.load().price_per_kg
        context.update(price_per_kg=price_per_kg, price=self.object.price_at(price_per_kg))
        return context


class PackageUpdateView(UpdateView):
    model = Package
    form_class = PackageForm
    template_name = "inventory/package_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Pacco #{self.object.pk} aggiornato.")
        return response

    def get_success_url(self):
        return reverse("inventory:detail", args=[self.object.pk])


class PackageSellView(UpdateView):
    """Vendita: il pacco passa allo storico. Sul pacco già venduto serve a correggere il prezzo."""

    model = Package
    form_class = SaleForm
    template_name = "inventory/package_sell.html"

    def get_initial(self):
        if self.object.is_sold:
            return {"sold_price": self.object.sold_price}
        return {"sold_price": self.object.price_at(ShopSettings.load().price_per_kg)}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        price_per_kg = ShopSettings.load().price_per_kg
        context.update(price_per_kg=price_per_kg, price=self.object.price_at(price_per_kg))
        return context

    def form_valid(self, form):
        package = form.instance
        self.first_sale = not package.is_sold  # serve a get_success_url, che super() chiama subito
        if self.first_sale:
            package.sold_at = timezone.now()
            package.sold_by = self.request.user
        response = super().form_valid(form)
        if self.first_sale:
            messages.success(
                self.request, f"Pacco #{package.pk} venduto a {euro(package.sold_price)} €: ora è nello storico."
            )
        else:
            messages.success(self.request, f"Prezzo del pacco #{package.pk} corretto: {euro(package.sold_price)} €.")
        return response

    def get_success_url(self):
        if self.first_sale:
            return stock_url(self.request)
        return reverse("inventory:detail", args=[self.object.pk])


@require_POST
def package_restore(request, pk):
    """Annulla una vendita: il pacco torna in magazzino."""
    package = get_object_or_404(Package.objects.sold(), pk=pk)
    package.sold_at = package.sold_price = package.sold_by = None
    package.save(update_fields=["sold_at", "sold_price", "sold_by", "updated_at"])
    messages.success(request, f"Pacco #{package.pk} rimesso in magazzino.")
    return redirect("inventory:detail", pk=package.pk)


class PackageDeleteView(DeleteView):
    """Eliminazione definitiva, per un pacco registrato per errore. Quelli venduti vanno nello storico."""

    model = Package
    template_name = "inventory/package_confirm_delete.html"

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


class BatchListView(ListView):
    template_name = "inventory/batch_list.html"
    context_object_name = "batches"

    def get_queryset(self):
        return Batch.objects.annotate(
            registered=Count("packages"),
            in_stock=Count("packages", filter=Q(packages__sold_at__isnull=True)),
            sold=Count("packages", filter=Q(packages__sold_at__isnull=False)),
        )


class BatchCreateView(CreateView):
    model = Batch
    form_class = BatchForm
    template_name = "inventory/batch_form.html"

    def get_initial(self):
        number = Batch.objects.count() + 1
        while Batch.objects.filter(name__iexact=f"Lotto {number}").exists():
            number += 1
        return {"name": f"Lotto {number}"}

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.object.expected_count:
            text = f"Lotto «{self.object.name}» creato: ora registra i suoi {self.object.expected_count} pacchi."
        else:
            text = f"Lotto «{self.object.name}» creato: registra i pacchi, il totale lo potrai indicare più avanti."
        messages.success(self.request, text)
        return response

    def get_success_url(self):
        return f"{reverse('inventory:create')}?lotto={self.object.pk}"


class BatchUpdateView(UpdateView):
    model = Batch
    form_class = BatchForm
    template_name = "inventory/batch_form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["from_register"] = self.request.GET.get("da") == "registra"
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Lotto «{self.object.name}» aggiornato.")
        return response

    def get_success_url(self):
        # Arrivando dalla registrazione si torna lì, a continuare con lo stesso lotto.
        if self.request.GET.get("da") == "registra":
            return f"{reverse('inventory:create')}?lotto={self.object.pk}"
        return reverse("inventory:batch_list")


class PriceUpdateView(UpdateView):
    """Prezzo al chilo: vale per i pacchi in magazzino. Le vendite passate tengono il loro prezzo."""

    model = ShopSettings
    form_class = PriceForm
    template_name = "inventory/price_form.html"

    def get_object(self, queryset=None):
        return ShopSettings.load()

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Prezzo aggiornato: {euro(self.object.price_per_kg)} € al kg.")
        return response

    def get_success_url(self):
        return stock_url(self.request)


@require_GET
def package_image(request, pk, kind):
    """Serve foto e miniatura solo a utenti autenticati (il middleware blocca gli anonimi)."""
    package = get_object_or_404(Package.objects.only("photo", "thumb"), pk=pk)
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
