"""I numeri della dashboard.

Le funzioni lavorano su queryset che il chiamante ha già limitato con lo `Scope`: qui non si
decide chi vede cosa. I giorni sono sempre quelli locali (Europe/Rome), non UTC.

Parole chiave:
- «Yakuza»: fatturato dei pacchi venduti senza scontrino.
- «Differenza»: nelle giornate chiuse, pacchi segnati con scontrino meno totale scontrini
  battuti in cassa. Positiva = in cassa meno scontrini di quelli che i pacchi fanno supporre.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

ZERO = Decimal("0.00")

PERIODS = [
    ("oggi", "Oggi"),
    ("7g", "7 giorni"),
    ("30g", "30 giorni"),
    ("mese", "Questo mese"),
    ("sempre", "Sempre"),
]
DEFAULT_PERIOD = "7g"
CHART_DAYS = 30  # giorni mostrati nel grafico
LIST_DAYS = 62  # giornate mostrate nell'elenco di un centro


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: date | None  # None = dall'inizio
    end: date  # incluso


def get_period(key, today=None, default=DEFAULT_PERIOD):
    today = today or timezone.localdate()
    labels = dict(PERIODS)
    if key not in labels:
        key = default
    start = {
        "oggi": today,
        "7g": today - timedelta(days=6),
        "30g": today - timedelta(days=29),
        "mese": today.replace(day=1),
        "sempre": None,
    }[key]
    return Period(key=key, label=labels[key], start=start, end=today)


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


@dataclass
class Totals:
    count: int = 0
    weight: Decimal = Decimal("0")
    revenue: Decimal = ZERO
    no_receipt: Decimal = ZERO  # «Yakuza»
    no_receipt_count: int = 0
    declared: Decimal = ZERO  # scontrini battuti in cassa, nelle giornate chiuse
    recorded_closed: Decimal = ZERO  # pacchi con scontrino nelle stesse giornate
    closed_days: int = 0
    missing_closes: int = 0  # giornate passate con vendite e senza chiusura

    @property
    def with_receipt(self):
        return self.revenue - self.no_receipt

    @property
    def with_receipt_count(self):
        return self.count - self.no_receipt_count

    @property
    def difference(self):
        return self.recorded_closed - self.declared

    @property
    def no_receipt_pct(self):
        return round(self.no_receipt * 100 / self.revenue) if self.revenue else 0

    def __iadd__(self, other):
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        return self


def daily_rows(packages):
    """Una riga per (centro, giorno) con almeno una vendita."""
    return list(
        packages.annotate(day=TruncDate("sold_at"))
        .values("store_id", "day")
        .annotate(
            count=Count("id"),
            weight=Sum("weight_kg"),
            revenue=Sum("sold_price"),
            no_receipt=Sum("sold_price", filter=Q(receipt=False)),
            no_receipt_count=Count("id", filter=Q(receipt=False)),
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
        totals.no_receipt += _money(row["no_receipt"])
        totals.no_receipt_count += row["no_receipt_count"]

    closed = set()
    for close in closes:
        totals = result[close.store_id]
        row = by_day.get((close.store_id, close.date))
        totals.declared += close.receipts_total
        if row:
            totals.recorded_closed += _money(row["revenue"]) - _money(row["no_receipt"])
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


def chart_days(rows, period, limit=CHART_DAYS):
    """Barre giornaliere (ultimi `limit` giorni del periodo, anche quelli senza vendite)."""
    first = period.end - timedelta(days=limit - 1)
    if period.start and period.start > first:
        first = period.start
    revenue = defaultdict(lambda: ZERO)
    no_receipt = defaultdict(lambda: ZERO)
    for row in rows:
        revenue[row["day"]] += _money(row["revenue"])
        no_receipt[row["day"]] += _money(row["no_receipt"])

    days = [first + timedelta(days=n) for n in range((period.end - first).days + 1)]
    peak = max((revenue[d] for d in days), default=ZERO)
    bars = []
    for day in days:
        total = revenue[day]
        bars.append(
            {
                "day": day,
                "revenue": total,
                "no_receipt": no_receipt[day],
                "height": round(total * 100 / peak) if peak else 0,
                "no_receipt_share": round(no_receipt[day] * 100 / total) if total else 0,
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
        no_receipt = _money(row["no_receipt"]) if row else ZERO
        items.append(
            {
                "day": day,
                "count": row["count"] if row else 0,
                "revenue": revenue,
                "no_receipt": no_receipt,
                "closed": close is not None,
                "declared": close.receipts_total if close else None,
                "difference": (revenue - no_receipt - close.receipts_total) if close else None,
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
