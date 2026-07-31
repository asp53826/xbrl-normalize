"""How often does normalisation actually work, and where does it fail?

Runs the full pipeline over a cross-sector sample and reports per-line coverage,
the direct/derived split, the revenue-tag distribution, and the accounting
identity as a correctness oracle.

    uv run python bench/coverage.py
"""

from __future__ import annotations

import asyncio
import collections
import os
import time

from xbrl_normalize.concepts import LINES
from xbrl_normalize.fetch import FetchError, Fetcher
from xbrl_normalize.normalize import NoAnnualData, normalize

# Deliberately spread across sectors: banks, insurers, energy, retail, pharma,
# industrials and utilities all present statements differently, and a sample of
# only large-cap tech would make the numbers look far better than they are.
SAMPLE = """
AAPL MSFT NVDA GOOGL AMZN META AVGO ORCL CRM ADBE CSCO INTC AMD QCOM TXN
JPM BAC WFC GS MS C SCHW BLK AXP USB PNC TFC COF
BRK-B PGR ALL MET PRU AIG TRV CB
XOM:34088 CVX COP SLB EOG PSX MPC VLO OXY KMI
WMT COST TGT HD LOW TJX DG KR NKE SBUX MCD
JNJ PFE MRK ABBV LLY UNH CVS CI TMO ABT BMY AMGN GILD
BA CAT DE HON GE LMT RTX UPS FDX UNP CSX MMM
NEE DUK SO D AEP EXC XEL
T VZ CMCSA DIS NFLX TMUS
PG KO PEP PM MO CL KMB GIS
"""


def pct(n, d):
    return f"{100*n/d:5.1f}%" if d else "    —"


async def main():
    os.environ.setdefault("EDGAR_USER_AGENT", "xbrl-normalize-bench bench@example.com")
    tickers = [t for t in SAMPLE.split() if t]

    t0 = time.perf_counter()
    async with Fetcher() as f:
        cik_map = await f.tickers()
        targets, skipped = [], []
        for t in tickers:
            if ":" in t:                      # explicit CIK override
                sym, cik = t.split(":")
                targets.append((sym, int(cik)))
                continue
            key = t.replace("-", ".")
            (targets.append((t, cik_map[key])) if key in cik_map else skipped.append(t))

        async def one(tk, cik):
            try:
                return tk, normalize(await f.company_facts(cik))
            except (FetchError, NoAnnualData, ValueError, KeyError) as e:
                return tk, e

        results = await asyncio.gather(*(one(t, c) for t, c in targets))
        elapsed = time.perf_counter() - t0
        downloads, hits, mb = f.downloads, f.hits, f.bytes_down / 1e6

    ok = [(t, s) for t, s in results if not isinstance(s, Exception)]
    failed = [(t, s) for t, s in results if isinstance(s, Exception)]
    n = len(ok)

    print(f"\nxbrl-normalize coverage — {n} companies")
    print(f"  fetched in {elapsed:.1f}s ({downloads} downloads, {hits} cached, {mb:.0f} MB)")
    if skipped:
        print(f"  not in SEC ticker file: {' '.join(skipped)}")
    for t, e in failed:
        print(f"  FAILED {t}: {type(e).__name__}: {e}")

    print(f"\n  {'line item':<30}{'coverage':>10}{'direct':>9}{'derived':>9}")
    print("  " + "-" * 58)
    for ln in LINES:
        have = [s for _, s in ok if ln.key in s.values]
        direct = sum(1 for s in have if s.values[ln.key].method == "direct")
        print(f"  {ln.label:<30}{pct(len(have), n):>10}{direct:>9}{len(have)-direct:>9}")

    print("\n  which tag answered 'revenue'")
    tags = collections.Counter(
        s.values["revenue"].source for _, s in ok if "revenue" in s.values
    )
    for tag, c in tags.most_common():
        print(f"    {c:>3}  {tag}")

    print("\n  accounting identity: assets = liabilities + equity")
    errs = [(t, s.identity_error()) for t, s in ok]
    checked = [(t, e) for t, e in errs if e is not None]
    exact = [t for t, e in checked if e < 1e-9]
    close = [t for t, e in checked if 1e-9 <= e < 1e-4]
    bad = [(t, e) for t, e in checked if e >= 1e-4]
    print(f"    checkable            {pct(len(checked), n)}  ({len(checked)}/{n})")
    print(f"    exact (< 1e-9)       {pct(len(exact), len(checked))}")
    print(f"    within 0.01%         {pct(len(close), len(checked))}")
    print(f"    off by more          {pct(len(bad), len(checked))}")
    for t, e in sorted(bad, key=lambda x: -x[1])[:10]:
        print(f"      {t:<8} off by {e:.2%}")

    worst = sorted(
        ((t, s) for t, s in ok), key=lambda x: -len(x[1].missing)
    )[:8]
    print("\n  companies with the most unresolved lines")
    for t, s in worst:
        print(f"    {t:<8} {len(s.missing):>2} missing: {', '.join(s.missing[:6])}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
