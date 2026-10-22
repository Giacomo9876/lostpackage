"""Chi può vedere e fare cosa.

Ogni richiesta di un utente autenticato ha un `Scope`:
- i titolari (e i superuser) vedono tutti i centri e tutti i punti in franchising;
- gli store manager vedono e toccano solo il proprio centro (e niente del franchising);
- chi è in franchising vede solo il proprio punto: il middleware lo lascia entrare solo nella
  sezione Franchising, e per lui tutti gli insiemi dei centri sono vuoti;
- chi non ha un profilo non entra: nessun accesso «per dimenticanza».

Le viste non filtrano mai da sole per centro o punto: partono sempre da `scope.packages()`,
`scope.franchise_days()` ecc. Un oggetto di un altro centro o punto è semplicemente inesistente (404).
"""
from dataclasses import dataclass
from datetime import timedelta

from django.shortcuts import redirect, render
from django.utils import timezone

from .models import Batch, DayClose, Franchise, FranchiseDay, Package, Profile, Store

# Quanti giorni indietro (oltre a oggi) uno store manager può inserire o correggere una vendita.
MANAGER_EDIT_DAYS = 1


@dataclass(frozen=True)
class Scope:
    is_owner: bool
    store: Store | None  # solo per gli store manager
    franchise: Franchise | None = None  # solo per chi è in franchising

    @classmethod
    def for_user(cls, user):
        if not (user.is_authenticated and user.is_active):
            return None
        profile = Profile.objects.select_related("store", "franchise").filter(user=user).first()
        if user.is_superuser or (profile and profile.is_owner):
            return cls(is_owner=True, store=None)
        if profile and profile.role == Profile.Role.MANAGER and profile.store_id:
            return cls(is_owner=False, store=profile.store)
        if profile and profile.role == Profile.Role.FRANCHISE and profile.franchise_id:
            return cls(is_owner=False, store=None, franchise=profile.franchise)
        return None

    @property
    def is_franchise(self):
        return self.franchise is not None

    @property
    def sees_franchising(self):
        return self.is_owner or self.is_franchise

    # Insiemi di dati visibili --------------------------------------------------
    # Chi non ha un centro (in franchising) riceve insiemi vuoti: mai «tutto» per errore.

    def _by_store(self, model):
        if self.is_owner:
            return model.objects.all()
        if self.store is None:
            return model.objects.none()
        return model.objects.filter(store=self.store)

    def stores(self):
        if self.is_owner:
            return Store.objects.all()
        return Store.objects.filter(pk=self.store.pk) if self.store else Store.objects.none()

    def packages(self):
        return self._by_store(Package)

    def batches(self):
        return self._by_store(Batch)

    def closes(self):
        return self._by_store(DayClose)

    def franchises(self):
        if self.is_owner:
            return Franchise.objects.all()
        if self.franchise is None:
            return Franchise.objects.none()
        return Franchise.objects.filter(pk=self.franchise.pk)

    def franchise_days(self):
        if self.is_owner:
            return FranchiseDay.objects.all()
        if self.franchise is None:
            return FranchiseDay.objects.none()
        return FranchiseDay.objects.filter(franchise=self.franchise)

    def resolve_franchise(self, raw):
        """Punto su cui si lavora: chi è in franchising ha sempre il suo; il titolare lo sceglie con ?punto=<id>."""
        if self.franchise is not None:
            return self.franchise
        if self.is_owner and raw is not None and str(raw).isdigit():
            return Franchise.objects.filter(pk=int(raw)).first()
        return None

    def resolve_store(self, raw):
        """Centro su cui si lavora. Il manager ha sempre il suo, qualunque cosa arrivi dalla richiesta.

        Il titolare lo sceglie con ?centro=<id>; senza una scelta valida restituisce None.
        """
        if not self.is_owner:
            return self.store
        if raw is not None and str(raw).isdigit():
            return Store.objects.filter(pk=int(raw)).first()
        return None

    # Correzioni ---------------------------------------------------------------

    def earliest_editable_day(self):
        """Primo giorno in cui questo utente può ancora inserire/correggere (None = nessun limite).

        Il titolare e chi è in franchising (sul proprio punto) possono inserire anche le giornate passate.
        """
        if self.is_owner or self.is_franchise:
            return None
        return timezone.localdate() - timedelta(days=MANAGER_EDIT_DAYS)

    def can_edit_day(self, day):
        earliest = self.earliest_editable_day()
        return earliest is None or earliest <= day <= timezone.localdate()

    def can_edit_sale(self, package):
        """Una vendita già registrata: il manager la corregge solo se è di oggi o di ieri."""
        return package.is_sold and (
            self.is_owner or self.can_edit_day(timezone.localdate(package.sold_at))
        )


# Le sole pagine dove entra chi è in franchising: tutto il resto lo riporta alla sua sezione.
FRANCHISE_URL_NAMES = {"franchise", "franchise_day", "franchise_photo", "franchise_delete", "receipt_read"}


class StoreAccessMiddleware:
    """Dopo il login: assegna `request.scope` oppure mostra «nessun accesso».

    Va messo dopo LoginRequiredMiddleware. Il pannello admin ha i suoi permessi e il logout
    deve funzionare sempre, anche per chi non ha un profilo. Chi è in franchising entra solo
    nelle pagine della sua sezione (elenco chiuso: una pagina nuova è vietata finché non la si aggiunge).
    """

    exempt_url_names = {"logout"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        request.scope = None
        if not request.user.is_authenticated:
            return None
        match = request.resolver_match
        if match and (match.app_name == "admin" or match.url_name in self.exempt_url_names):
            return None
        request.scope = Scope.for_user(request.user)
        if request.scope is None:
            return render(request, "no_access.html", status=403)
        if request.scope.is_franchise and not (
            match and match.namespace == "inventory" and match.url_name in FRANCHISE_URL_NAMES
        ):
            return redirect("inventory:franchise")
        return None

