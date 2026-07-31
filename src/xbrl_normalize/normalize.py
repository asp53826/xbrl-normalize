"""Turn a companyfacts blob into comparable statements.

Three problems sit between the raw facts and a usable number:

1. **Tag choice.** Filers disagree on which us-gaap tag carries a line, and
   change their minds between eras.
2. **Staleness.** A tag existing in the facts says nothing about whether it was
   used recently. Microsoft's `Revenues` last carried a value in 2010.
3. **Restatement.** The same fiscal period is reported repeatedly across
   filings, with revisions. The latest filing wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .concepts import BY_KEY, DERIVATIONS_BY_TARGET, DURATION, INSTANT, LINES, Line

ANNUAL_MIN, ANNUAL_MAX = 330, 400
QUARTER_MIN, QUARTER_MAX = 80, 100


@dataclass(frozen=True)
class Obs:
    val: float
    start: str | None
    end: str
    filed: str
    form: str
    fy: int | None
    fp: str | None

    @property
    def days(self) -> int:
        if not self.start:
            return 0
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days


@dataclass(frozen=True)
class Period:
    end: str
    start: str | None
    kind: str
    fy: int | None = None

    def __str__(self) -> str:
        return f"FY{self.fy or self.end[:4]} ending {self.end}"


@dataclass(frozen=True)
class Value:
    value: float
    method: str           # "direct" or "derived"
    source: str           # tag name, or the formula used
    filed: str | None = None
    form: str | None = None


@dataclass
class Statements:
    cik: int
    name: str
    period: Period
    values: dict[str, Value]
    missing: list[str]

    def get(self, key: str) -> float | None:
        v = self.values.get(key)
        return v.value if v else None

    def identity_error(self) -> float | None:
        """Relative residual of assets = liabilities + temporary equity + equity.

        The oracle for whether normalisation produced a coherent balance sheet.
        Mezzanine equity is counted because it belongs to neither side and most
        filers that carry it would otherwise look broken. Companies without it
        contribute zero.
        """
        a, l, e = self.get("assets"), self.get("liabilities"), self.get("equity")
        if a is None or l is None or e is None or not a:
            return None
        t = self.get("temporary_equity") or 0.0
        return abs(a - (l + t + e)) / abs(a)


class Facts:
    """Indexed view over one company's us-gaap facts."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.cik = raw.get("cik", 0)
        self.name = raw.get("entityName", "")
        self._us = raw.get("facts", {}).get("us-gaap", {})
        self._cache: dict[tuple[str, str], list[Obs]] = {}

    def has(self, tag: str) -> bool:
        return tag in self._us

    def observations(self, tag: str, unit: str = "USD") -> list[Obs]:
        key = (tag, unit)
        if key not in self._cache:
            pts = self._us.get(tag, {}).get("units", {}).get(unit, [])
            self._cache[key] = [
                Obs(p["val"], p.get("start"), p["end"], p.get("filed", ""),
                    p.get("form", ""), p.get("fy"), p.get("fp"))
                for p in pts if p.get("val") is not None and p.get("end")
            ]
        return self._cache[key]

    def annual_periods(self) -> list[Period]:
        """Fiscal years the company actually reported, newest first.

        Built from the union of every annual-length duration rather than one
        anchor tag, because no single tag is present for every filer.
        """
        spans: dict[tuple[str, str], int] = {}
        for tag in self._us:
            for o in self.observations(tag):
                if o.start and ANNUAL_MIN <= o.days <= ANNUAL_MAX:
                    spans[(o.start, o.end)] = spans.get((o.start, o.end), 0) + 1

        # A fiscal year end can appear with slightly different starts (52/53-week
        # calendars). Keep the most-attested span per end date.
        best: dict[str, tuple[str, int]] = {}
        for (start, end), n in spans.items():
            if end not in best or n > best[end][1]:
                best[end] = (start, n)

        return [
            Period(end=end, start=start, kind=DURATION, fy=int(end[:4]))
            for end, (start, _) in sorted(best.items(), reverse=True)
        ]

    def pick(self, tag: str, period: Period, kind: str) -> Obs | None:
        """Best observation of `tag` for `period`, resolving restatements by
        taking the most recently filed."""
        cands = []
        for o in self.observations(tag):
            if kind == INSTANT:
                if o.start is None and o.end == period.end:
                    cands.append(o)
            else:
                if o.start and o.end == period.end and ANNUAL_MIN <= o.days <= ANNUAL_MAX:
                    cands.append(o)
        if not cands:
            return None
        # newest filing wins; 10-K outranks 10-Q for the same filing date
        cands.sort(key=lambda o: (o.filed, o.form.startswith("10-K")))
        return cands[-1]


class Resolver:
    def __init__(self, facts: Facts, period: Period):
        self.f = facts
        self.p = period
        self._memo: dict[str, Value | None] = {}
        self._active: set[str] = set()

    def resolve(self, key: str) -> Value | None:
        if key in self._memo:
            return self._memo[key]
        if key in self._active:
            return None  # liabilities <-> equity would otherwise recurse forever
        self._active.add(key)
        try:
            out = self._direct(key) or self._derived(key)
        finally:
            self._active.discard(key)
        self._memo[key] = out
        return out

    def _direct(self, key: str) -> Value | None:
        line: Line = BY_KEY[key]
        for tag in line.tags:
            o = self.f.pick(tag, self.p, line.kind)
            if o is not None:
                return Value(o.val, "direct", tag, o.filed, o.form)
        return None

    def _derived(self, key: str) -> Value | None:
        for d in DERIVATIONS_BY_TARGET.get(key, ()):
            parts = [self.resolve(i) for i in d.inputs]
            if any(p is None for p in parts):
                continue
            args = [p.value for p in parts]
            # optional inputs contribute 0 when the filer doesn't report them
            args += [(v.value if (v := self.resolve(o)) else 0.0) for o in d.optional]
            try:
                val = d.fn(*args)
            except (TypeError, ZeroDivisionError):
                continue
            return Value(val, "derived", d.op)
        return None


class NoAnnualData(ValueError):
    """The registrant has no annual XBRL history to normalize."""


def normalize(raw: dict, fy: int | None = None) -> Statements:
    """Normalize one company. `fy=None` takes the most recent fiscal year."""
    facts = Facts(raw)
    periods = facts.annual_periods()
    if not periods:
        # A ticker resolves to the *current* registrant, which after a corporate
        # reorganisation can be a brand-new CIK with no history. XOM points at
        # CIK 2115436, whose companyfacts is empty; the history sits under the
        # predecessor CIK 34088. Say so rather than reporting every line missing.
        raise NoAnnualData(
            f"CIK {facts.cik} ({facts.name or 'unknown'}) has no annual XBRL data. "
            "If this ticker recently reorganised, pass the predecessor CIK directly."
        )

    period = periods[0] if fy is None else next(
        (p for p in periods if p.fy == fy), None
    )
    if period is None:
        raise ValueError(
            f"{facts.name}: no annual period for FY{fy}; "
            f"available {[p.fy for p in periods[:8]]}"
        )

    # Instants are read at the fiscal year end of the chosen duration. Two
    # resolvers so each keeps its own memo across every line of its kind.
    resolvers = {
        DURATION: Resolver(facts, period),
        INSTANT: Resolver(facts, Period(period.end, None, INSTANT, period.fy)),
    }

    values, missing = {}, []
    for line in LINES:
        v = resolvers[line.kind].resolve(line.key)
        if v is None:
            missing.append(line.key)
        else:
            values[line.key] = v
    return Statements(facts.cik, facts.name, period, values, missing)
