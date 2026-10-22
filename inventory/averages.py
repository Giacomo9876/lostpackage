"""Le tre medie degli incassi, calcolate dalle chiusure di giornata.

Ogni chiusura ha gli scontrini battuti e (solo per i titolari, che li vedono) i contanti Yakuza; la
giornata vale la somma. Si calcolano tre medie:

- **infrasettimanale**: media al giorno da lunedì a venerdì;
- **settimana**: media al giorno su tutti i giorni, e incasso medio di una settimana intera;
- **fine settimana**: media al giorno di sabato e domenica.

Una media usa solo le giornate in cui c'è una chiusura: un giorno senza chiusura è un dato che non c'è,
non uno zero. Le settimane intere sono quelle da lunedì a domenica già concluse e dentro al periodo; le
settimane tagliate dal periodo (o quella in corso) non entrano, per non abbassare la media.

Chi chiama passa le righe già limitate dallo `Scope`: qui non si decide chi vede cosa.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from . import stats

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

WEEKEND_DAYS = (5, 6)  # sabato e domenica (date.weekday())

PERIODS = [
    ("14g", "2 settimane"),
    ("30g", "1 mese"),
    ("60g", "2 mesi"),
    ("90g", "3 mesi"),
    ("sempre", "Sempre"),
]
DEFAULT_PERIOD = "90g"
LIST_LIMIT = 60  # giornate mostrate nell'elenco


def get_period(key, today=None):
    """Il periodo scelto, tra quelli delle medie (altrimenti il predefinito)."""
    if key not in dict(PERIODS):
        key = DEFAULT_PERIOD
    return stats.get_period(key, today=today)


def _mean(total, count):
    if not count:
        return None
    return (Decimal(total) / Decimal(count)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Bucket:
    """Le giornate di un gruppo (lun–ven, sab–dom, tutte) con le loro somme."""

    days: int = 0
    receipts: Decimal = ZERO
    yakuza: Decimal = ZERO

    def add(self, receipts, yakuza):
        self.days += 1
        self.receipts += receipts
        self.yakuza += yakuza

    @property
    def total(self):
        return self.receipts + self.yakuza

    @property
    def avg_receipts(self):
        return _mean(self.receipts, self.days)

    @property
    def avg_yakuza(self):
        return _mean(self.yakuza, self.days)

    @property
    def avg_total(self):
        return _mean(self.total, self.days)


@dataclass
class Averages:
    weekdays: Bucket = field(default_factory=Bucket)  # lunedì–venerdì
    weekend: Bucket = field(default_factory=Bucket)  # sabato e domenica
    all_days: Bucket = field(default_factory=Bucket)  # tutta la settimana
    weeks: int = 0  # settimane intere usate per l'incasso medio di una settimana
    week_receipts: Decimal | None = None
    week_yakuza: Decimal | None = None

    @property
    def week_total(self):
        if self.week_receipts is None:
            return None
        return self.week_receipts + self.week_yakuza

    @property
    def has_data(self):
        return self.all_days.days > 0


@dataclass
class ChartRow:
    """Una barra del grafico delle medie: la media al giorno di un gruppo di giorni."""

    title: str
    note: str
    bucket: Bucket
    pct: int = 0  # lunghezza della barra: la media più alta delle tre vale 100
    yakuza_pct: int = 0  # quanta parte della barra è Yakuza (in % della barra)


def chart_rows(average):
    """Le tre medie al giorno pronte per il grafico a barre, nello stesso ordine della settimana."""
    rows = [
        ChartRow("Infrasettimanale", "lun – ven", average.weekdays),
        ChartRow("Settimanale", "tutti i giorni", average.all_days),
        ChartRow("Fine settimana", "sab – dom", average.weekend),
    ]
    top = max((row.bucket.avg_total for row in rows if row.bucket.avg_total), default=None)
    for row in rows:
        total = row.bucket.avg_total
        if top and total:
            row.pct = max(2, round(total / top * 100))  # una media piccola resta comunque visibile
            if row.bucket.avg_yakuza:
                row.yakuza_pct = min(100, max(1, round(row.bucket.avg_yakuza / total * 100)))
    return rows


def closing_rows(closes, period):
    """Le chiusure del periodo come righe (centro, giorno, scontrini, Yakuza), pronte per `compute`."""
    return list(stats.closes_in(closes, period).values_list("store_id", "date", "receipts_total", "yakuza_cash"))


def compute(rows, period, today):
    """Le tre medie dalle righe `(centro, giorno, scontrini, Yakuza)` di un solo centro (o di tutti insieme)."""
    result = Averages()
    daily = defaultdict(lambda: [ZERO, ZERO])
    for _, day, receipts, yakuza in rows:
        daily[day][0] += receipts
        daily[day][1] += yakuza

    for day, (receipts, yakuza) in daily.items():
        result.all_days.add(receipts, yakuza)
        (result.weekend if day.weekday() in WEEKEND_DAYS else result.weekdays).add(receipts, yakuza)

    # Settimane intere: da lunedì a domenica, tutte dentro al periodo e già finite.
    first = period.start or (min(daily) if daily else None)
    weeks = defaultdict(lambda: [ZERO, ZERO])
    for day, (receipts, yakuza) in daily.items():
        monday = day - timedelta(days=day.weekday())
        sunday = monday + timedelta(days=6)
        if first is not None and monday >= first and sunday < today and sunday <= period.end:
            weeks[monday][0] += receipts
            weeks[monday][1] += yakuza
    if weeks:
        result.weeks = len(weeks)
        result.week_receipts = _mean(sum((w[0] for w in weeks.values()), ZERO), len(weeks))
        result.week_yakuza = _mean(sum((w[1] for w in weeks.values()), ZERO), len(weeks))
    return result


def compute_by_store(rows, period, today):
    """Le medie di ogni centro: {id del centro: Averages}."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[0]].append(row)
    return {store_id: compute(items, period, today) for store_id, items in grouped.items()}
