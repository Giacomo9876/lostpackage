"""Sezione Risorse: come sono messi pallet/merce, sticker e scotch in ogni centro e punto in franchising.

Lo store manager aggiorna solo il proprio centro, chi è in franchising solo il proprio punto: per loro il
posto è fisso e quello che arriva nella richiesta (?centro=, ?punto=) non conta. I titolari vedono tutto
(filtrabile per centri e franchising) e possono modificare qualunque centro o punto scegliendolo con
?centro=<id> o ?punto=<id>. Il perimetro lo decidono `Scope.supplies()`, `Scope.supply_place` e i resolver.
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views.generic import TemplateView

from .forms import SupplyForm
from .models import Franchise, Store, SupplyItem, SupplyStatus

KINDS = [("", "Tutti"), ("centri", "Centri commerciali"), ("franchising", "Franchising")]


def summary(statuses):
    """Quante risorse sono finite (rosso) e in esaurimento (giallo) tra quelle attive."""
    return statuses.filter(item__active=True).aggregate(
        red=Count("id", filter=Q(level=SupplyStatus.Level.RED)),
        yellow=Count("id", filter=Q(level=SupplyStatus.Level.YELLOW)),
    )


class SuppliesView(TemplateView):
    template_name = "inventory/supplies.html"

    def items(self):
        return SupplyItem.objects.filter(active=True)

    def editing_place(self):
        """Il centro o punto da modificare: fisso per manager e franchising, scelto dal titolare (o nessuno)."""
        scope = self.request.scope
        if scope.supply_place is not None:
            return scope.supply_place
        if scope.is_owner:
            return scope.resolve_store(self.request.GET.get("centro")) or scope.resolve_franchise(
                self.request.GET.get("punto")
            )
        return None

    def place_statuses(self, place):
        key = "store" if isinstance(place, Store) else "franchise"
        statuses = self.request.scope.supplies().filter(**{key: place}).select_related("updated_by")
        return {status.item_id: status for status in statuses}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        place = self.editing_place()
        if place is not None:
            form = kwargs.get("form") or SupplyForm(items=self.items(), statuses=self.place_statuses(place))
            context.update(place=place, form=form, overview=False, as_owner=self.request.scope.is_owner)
            return context
        context.update(overview=True, **self.overview())
        return context

    def overview(self):
        """Per i titolari: ogni centro e punto con le sue risorse; filtri per tipo e «solo da sistemare»."""
        request = self.request
        if not request.scope.is_owner:
            raise PermissionDenied  # l'elenco di tutti i posti è solo per i titolari
        kind = request.GET.get("tipo", "")
        kind = kind if kind in dict(KINDS) else ""
        only_problems = request.GET.get("solo") == "problemi"
        items = list(self.items())
        statuses = request.scope.supplies().filter(item__active=True).select_related("updated_by")
        by_store, by_franchise = {}, {}
        for status in statuses:
            target = by_store if status.store_id else by_franchise
            target.setdefault(status.store_id or status.franchise_id, {})[status.item_id] = status

        def cards(places, found):
            result = []
            for place in places:
                rows = [(item, found.get(place.pk, {}).get(item.pk)) for item in items]
                if only_problems:
                    rows = [(item, st) for item, st in rows if st and st.level != SupplyStatus.Level.GREEN]
                    if not rows:
                        continue
                param = "centro" if isinstance(place, Store) else "punto"
                result.append({"place": place, "rows": rows, "edit_url": f"{reverse('inventory:supplies')}?{param}={place.pk}"})
            return result

        groups = []
        if kind in ("", "centri"):
            groups.append({"title": "Centri commerciali", "cards": cards(Store.objects.all(), by_store)})
        if kind in ("", "franchising"):
            groups.append({"title": "Franchising", "cards": cards(Franchise.objects.all(), by_franchise)})
        return {
            "groups": groups,
            "kinds": KINDS,
            "kind": kind,
            "only_problems": only_problems,
            "totals": summary(request.scope.supplies()),
        }

    def post(self, request, *args, **kwargs):
        place = self.editing_place()
        if place is None:
            raise PermissionDenied  # nessun centro o punto da modificare (titolare senza scelta, o nessun ruolo)
        form = SupplyForm(request.POST, items=self.items(), statuses=self.place_statuses(place))
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        changed = form.save(place, request.user)
        where = f" di {place.name}" if request.scope.is_owner else ""
        if changed:
            messages.success(request, f"Risorse{where} aggiornate ({changed}).")
        else:
            messages.info(request, f"Nessuna modifica: le risorse{where} erano già così.")
        return HttpResponseRedirect(reverse("inventory:supplies"))
