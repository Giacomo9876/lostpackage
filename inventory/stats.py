"""I numeri della dashboard.

Le funzioni lavorano su queryset che il chiamante ha già limitato con lo `Scope`: qui non si
decide chi vede cosa. I giorni sono sempre quelli locali (Europe/Rome), non UTC.

Parole chiave:
- «Yakuza»: fatturato dei pacchi venduti con pagamento «Yakuza» (senza scontrino). Gli altri
  pagamenti (contanti, carta, altro) hanno lo scontrino.
- «Differenza»: nelle giornate chiuse, pacchi con scontrino meno totale scontrini
  battuti in cassa. Positiva = in cassa meno scontrini di quelli che i pacchi fanno supporre.
- «Sconti»: dove chi vende ha scritto un prezzo più basso del listino, la differenza (solo vendite
  registrate col listino; le più vecchie non hanno il dato).
"""
import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, DecimalField, F, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import Package

ZERO = Decimal("0.00")

PERIODS = [
    ("oggi", "Oggi"),
    ("3g", "3 giorni"),
    ("7g", "7 giorni"),
    ("14g", "2 settimane"),
    ("30g", "1 mese"),
    ("60g", "2 mesi"),
    ("90g", "3 mesi"),
    ("mese", "Questo mese"),
    ("sempre", "Sempre"),
]
# Periodi «ultimi N giorni», oggi compreso.
PERIOD_DAYS = {"oggi": 1, "3g": 3, "7g": 7, "14g": 14, "30g": 30, "60g": 60, "90g": 90}
DEFAULT_PERIOD = "7g"
CHART_DAYS = 90  # giorni mostrati al massimo nel grafico
LIST_DAYS = 62  # giornate mostrate nell'elenco di un centro


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: date | None  # None = dall'inizio
    end: date  # incluso

    @property
    def days(self):
        return (self.end - self.start).days + 1 if self.start else None


def get_period(key, today=None, default=DEFAULT_PERIOD):
    today = today or timezone.localdate()
    labels = dict(PERIODS)
    if key not in labels:
        key = default
    if key in PERIOD_DAYS:
        start = today - timedelta(days=PERIOD_DAYS[key] - 1)
    elif key == "mese":
        start = today.replace(day=1)
    else:
        start = None
    return Period(key=key, label=labels[key], start=start, end=today)


def compare_with(period):
    """Il periodo con cui confrontare quello scelto, e come chiamarlo: (Period, testo) oppure None.

    Per gli ultimi N giorni sono gli N giorni subito prima; per «Questo mese» gli stessi giorni del mese
    scorso. «Oggi» e «Sempre» non si confrontano: il giorno non è finito, e prima di «sempre» non c'è nulla.
    """
    if period.key in ("oggi", "sempre"):
        return None
    if period.key == "mese":
        first = (period.start - timedelta(days=1)).replace(day=1)
        last_day = min(period.days, calendar.monthrange(first.year, first.month)[1])
        before = Period(key="mese-prima", label="", start=first, end=first.replace(day=last_day))
        return before, "rispetto allo stesso periodo del mese scorso"
    end = period.start - timedelta(days=1)
    before = Period(key=f"{period.key}-prima", label="", start=end - timedelta(days=period.days - 1), end=end)
    return before, f"rispetto ai {period.days} giorni precedenti"


def change_pct(now, before):
    """Variazione percentuale intera rispetto a `before`; None se non c'è un termine di paragone."""
    if not before:
        return None
    return int(((Decimal(now) - Decimal(before)) * 100 / Decimal(before)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def start_of_day(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def sold_in(packages, period):
    """Pacchi venduti nel periodo (giorni locali)."""
    packages = packages.filter(sold_at__isnull=False, sold_at__lt=start_of_day(period.end + timedelta(days=1)))
    if period.start:
        packages = packages.filter(sold_at__gte=start_of_day(period.start))
    return packages


def closes_in(closes, period):
    closes = closes.filter(date__lte=period.end)
    if period.start:
        closes = closes.filter(date__gte=period.start)
    return closes


def _money(value):
    return ZERO if value is None else value


PAYMENTS = [  # nell'ordine in cui si mostrano: (valore, nome del campo in Totals)
    (Package.Payment.CASH, "cash"),
    (Package.Payment.CARD, "card"),
    (Package.Payment.OTHER, "other"),
    (Package.Payment.YAKUZA, "yakuza"),
]


@dataclass
class Totals:
    count: int = 0
    weight: Decimal = Decimal("0")
    revenue: Decimal = ZERO
    cash: Decimal = ZERO  # incasso per metodo di pagamento
    card: Decimal = ZERO
    other: Decimal = ZERO
    yakuza: Decimal = ZERO  # «Yakuza»
    yakuza_count: int = 0
    premium_count: int = 0
    discount: Decimal = ZERO  # somma degli sconti rispetto al listino
    discount_count: int = 0
    declared: Decimal = ZERO  # scontrini battuti in cassa, nelle giornate chiuse
    recorded_closed: Decimal = ZERO  # pacchi con scontrino nelle stesse giornate
    closed_days: int = 0
    missing_closes: int = 0  # giornate passate con vendite e senza chiusura

    @property
    def with_receipt(self):
        return self.revenue - self.yakuza

    @property
    def with_receipt_count(self):
        return self.count - self.yakuza_count

    @property
    def difference(self):
        return self.recorded_closed - self.declared

    @property
    def yakuza_pct(self):
        return round(self.yakuza * 100 / self.revenue) if self.revenue else 0

    @property
    def avg_price(self):
        """Incasso medio per pacco."""
        return _ratio(self.revenue, self.count)

    @property
    def avg_weight(self):
        """Peso medio di un pacco, in kg."""
        return _ratio(self.weight, self.count)

    @property
    def price_per_kg(self):
        """Incasso medio al kg (i pacchi piccoli a prezzo fisso lo alzano)."""
        return _ratio(self.revenue, self.weight)

    @property
    def premium_pct(self):
        return round(self.premium_count * 100 / self.count) if self.count else 0

    @property
    def mix(self):
        """Ripartizione dell'incasso per pagamento, per la barra e la legenda."""
        result = []
        for value, name in PAYMENTS:
            amount = getattr(self, name)
            result.append(
                {
                    "key": value,
                    "label": Package.Payment(value).label,
                    "amount": amount,
                    "pct": round(amount * 100 / self.revenue) if self.revenue else 0,
                    # Larghezza relativa in centesimi interi: la barra non ha bisogno che le percentuali sommino 100.
                    "weight": int(amount * 100),
                }
            )
        return result

    def __iadd__(self, other):
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        return self


def _ratio(total, count):
    if not count:
        return None
    return (Decimal(total) / Decimal(count)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


_DISCOUNTED = Q(list_price__gt=F("sold_price"))


def _sum_if(condition):
    return Sum("sold_price", filter=condition)


def daily_rows(packages):
    """Una riga per (centro, giorno) con almeno una vendita."""
    by_payment = {
        name: _sum_if(Q(payment=value)) for value, name in PAYMENTS
    }
    return list(
        packages.annotate(day=TruncDate("sold_at"))
        .values("store_id", "day")
        .annotate(
            count=Count("id"),
            weight=Sum("weight_kg"),
            revenue=Sum("sold_price"),
            yakuza_count=Count("id", filter=Q(payment=Package.Payment.YAKUZA)),
            premium_count=Count("id", filter=Q(premium=True)),
            discount=Sum(
                F("list_price") - F("sold_price"),
                filter=_DISCOUNTED,
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ),
            discount_count=Count("id", filter=_DISCOUNTED),
            **by_payment,
        )
        .order_by("day", "store_id")
    )


def totals_by_store(rows, closes, today):
    """Totali per centro: {store_id: Totals}. `closes` sono oggetti DayClose."""
    result = defaultdict(Totals)
    by_day = {(row["store_id"], row["day"]): row for row in rows}
    for row in rows:
        totals = result[row["store_id"]]
        totals.count += row["count"]
        totals.weight += row["weight"] or Decimal("0")
        totals.revenue += _money(row["revenue"])
        for _, name in PAYMENTS:
            setattr(totals, name, getattr(totals, name) + _money(row[name]))
        totals.yakuza_count += row["yakuza_count"]
        totals.premium_count += row["premium_count"]
        totals.discount += _money(row["discount"])
        totals.discount_count += row["discount_count"]

    closed = set()
    for close in closes:
        totals = result[close.store_id]
        row = by_day.get((close.store_id, close.date))
        totals.declared += close.receipts_total
        if row:
            totals.recorded_closed += _money(row["revenue"]) - _money(row["yakuza"])
        totals.closed_days += 1
        closed.add((close.store_id, close.date))

    for (store_id, day) in by_day:
        if day < today and (store_id, day) not in closed:
            result[store_id].missing_closes += 1
    return result


def sum_totals(per_store):
    total = Totals()
    for totals in per_store.values():
        total += totals
    return total


@dataclass
class Insights:
    """Qualche numero che racconta l'andamento nel periodo."""

    daily_avg: Decimal | None  # incasso medio al giorno (giorni del periodo, anche senza vendite)
    best_day: date | None
    best_revenue: Decimal = ZERO
    active_days: int = 0  # giorni con almeno una vendita


def insights(rows, totals, period, today=None):
    today = today or timezone.localdate()
    revenue_by_day = defaultdict(lambda: ZERO)
    for row in rows:
        revenue_by_day[row["day"]] += _money(row["revenue"])
    if period.start:
        days = period.days
    elif revenue_by_day:
        days = (period.end - min(revenue_by_day)).days + 1
    else:
        days = 0
    best_day = max(revenue_by_day, key=lambda d: (revenue_by_day[d], d), default=None)
    return Insights(
        daily_avg=_ratio(totals.revenue, days),
        best_day=best_day,
        best_revenue=revenue_by_day[best_day] if best_day else ZERO,
        active_days=len(revenue_by_day),
    )


def _label_step(count):
    """Ogni quanti giorni mettere la data sotto una barra, perché le date non si sovrappongano."""
    if count <= 8:
        return 1
    if count <= 16:
        return 2
    if count <= 35:
        return 5
    return 10


def chart_days(rows, period, limit=CHART_DAYS):
    """Barre giornaliere (ultimi `limit` giorni del periodo, anche quelli senza vendite)."""
    first = period.end - timedelta(days=limit - 1)
    if period.start and period.start > first:
        first = period.start
    revenue = defaultdict(lambda: ZERO)
    yakuza = defaultdict(lambda: ZERO)
    for row in rows:
        revenue[row["day"]] += _money(row["revenue"])
        yakuza[row["day"]] += _money(row["yakuza"])

    days = [first + timedelta(days=n) for n in range((period.end - first).days + 1)]
    peak = max((revenue[d] for d in days), default=ZERO)
    step = _label_step(len(days))
    bars = []
    for index, day in enumerate(days):
        total = revenue[day]
        from_end = len(days) - 1 - index
        bars.append(
            {
                "day": day,
                "revenue": total,
                "yakuza": yakuza[day],
                "height": round(total * 100 / peak) if peak else 0,
                "yakuza_share": round(yakuza[day] * 100 / total) if total else 0,
                # Prima e ultima barra hanno sempre la data; le altre ogni `step`, senza stare addosso all'ultima.
                "label": index == 0 or from_end == 0 or (index % step == 0 and from_end >= step // 2 + 1),
            }
        )
    return bars


def day_list(rows, closes, limit=LIST_DAYS):
    """Elenco giornate di un centro, dalla più recente: vendite e chiusura."""
    by_day = {row["day"]: row for row in rows}
    close_by_day = {close.date: close for close in closes}
    items = []
    for day in sorted(set(by_day) | set(close_by_day), reverse=True)[:limit]:
        row = by_day.get(day)
        close = close_by_day.get(day)
        revenue = _money(row["revenue"]) if row else ZERO
        yakuza = _money(row["yakuza"]) if row else ZERO
        items.append(
            {
                "day": day,
                "count": row["count"] if row else 0,
                "revenue": revenue,
                "yakuza": yakuza,
                "discount": _money(row["discount"]) if row else ZERO,
                "closed": close is not None,
                "declared": close.receipts_total if close else None,
                "difference": (revenue - yakuza - close.receipts_total) if close else None,
            }
        )
    return items


def stock_by_store(in_stock, shop):
    """Pacchi in magazzino per centro: numero, peso e valore secondo le regole di prezzo correnti."""
    result = defaultdict(lambda: {"count": 0, "weight": Decimal("0"), "value": ZERO})
    rows = in_stock.order_by().values_list("store_id", "weight_kg", "premium").iterator(chunk_size=2000)
    for store_id, weight, premium in rows:
        entry = result[store_id]
        entry["count"] += 1
        entry["weight"] += weight
        entry["value"] += shop.price_for(weight, premium)
    return result
