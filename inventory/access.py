"""Chi può vedere e fare cosa.

Ogni richiesta di un utente autenticato ha un `Scope`:
- i titolari (e i superuser) vedono tutti i centri;
- gli store manager vedono e toccano solo il proprio centro;
- chi non ha un profilo non entra: nessun accesso «per dimenticanza».

Le viste non filtrano mai da sole per centro: partono sempre da `scope.packages()`,
`scope.batches()` ecc. Un oggetto di un altro centro è semplicemente inesistente (404).
"""
from dataclasses import dataclass
from datetime import timedelta

from django.shortcuts import render
from django.utils import timezone

from .models import Batch, DayClose, Package, Profile, Store

# Quanti giorni indietro (oltre a oggi) uno store manager può inserire o correggere una vendita.
MANAGER_EDIT_DAYS = 1


@dataclass(frozen=True)
class Scope:
    is_owner: bool
    store: Store | None  # solo per gli store manager

    @classmethod
    def for_user(cls, user):
        if not (user.is_authenticated and user.is_active):
            return None
        profile = Profile.objects.select_related("store").filter(user=user).first()
        if user.is_superuser or (profile and profile.is_owner):
            return cls(is_owner=True, store=None)
        if profile and profile.store_id:
            return cls(is_owner=False, store=profile.store)
        return None

    # Insiemi di dati visibili --------------------------------------------------

    def stores(self):
        return Store.objects.all() if self.is_owner else Store.objects.filter(pk=self.store.pk)

    def packages(self):
        return Package.objects.all() if self.is_owner else Package.objects.filter(store=self.store)

    def batches(self):
        return Batch.objects.all() if self.is_owner else Batch.objects.filter(store=self.store)

    def closes(self):
        return DayClose.objects.all() if self.is_owner else DayClose.objects.filter(store=self.store)

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
        """Primo giorno in cui questo utente può ancora inserire/correggere vendite (None = nessun limite)."""
        if self.is_owner:
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


class StoreAccessMiddleware:
    """Dopo il login: assegna `request.scope` oppure mostra «nessun accesso».

    Va messo dopo LoginRequiredMiddleware. Il pannello admin ha i suoi permessi e il logout
    deve funzionare sempre, anche per chi non ha un profilo.
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
        return None

