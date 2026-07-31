from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .concepts import LINES
from .fetch import Fetcher, FetchError
from .normalize import normalize

STATEMENTS = ("income", "balance", "cashflow")


async def _run(args) -> int:
    async with Fetcher() as f:
        target = args.company.strip().upper()
        if target.isdigit():
            cik = int(target)
        else:
            tickers = await f.tickers()
            if target not in tickers:
                print(f"unknown ticker {target!r}", file=sys.stderr)
                return 2
            cik = tickers[target]
        raw = await f.company_facts(cik)

    st = normalize(raw, fy=args.fy)

    if args.json:
        print(json.dumps({
            "cik": st.cik,
            "name": st.name,
            "period": {"fy": st.period.fy, "start": st.period.start, "end": st.period.end},
            "identity_error": st.identity_error(),
            "values": {
                k: {"value": v.value, "method": v.method, "source": v.source,
                    "filed": v.filed, "form": v.form}
                for k, v in st.values.items()
            },
            "missing": st.missing,
        }, indent=2))
        return 0

    print(f"{st.name}  (CIK {st.cik})")
    print(f"{st.period}\n")
    for stmt in STATEMENTS:
        rows = [ln for ln in LINES if ln.statement == stmt]
        if not any(ln.key in st.values for ln in rows):
            continue
        print(f"  {stmt.upper()}")
        for ln in rows:
            v = st.values.get(ln.key)
            if v is None:
                print(f"    {ln.label:<32}{'—':>16}")
                continue
            note = "" if v.method == "direct" else f"  [{v.source}]"
            print(f"    {ln.label:<32}{v.value/1e6:>15,.0f}M{note}")
        print()

    err = st.identity_error()
    if err is not None:
        ok = "balances" if err < 1e-6 else f"OFF by {err:.2%}"
        print(f"  assets = liabilities + equity: {ok}")
    if st.missing and args.verbose:
        print(f"  unresolved: {', '.join(st.missing)}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        prog="xbrl-normalize",
        description="Normalize SEC XBRL facts into comparable financial statements.",
    )
    p.add_argument("company", help="ticker or CIK")
    p.add_argument("--fy", type=int, help="fiscal year (default: most recent)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except FetchError as e:
        print(e, file=sys.stderr)
        raise SystemExit(1)
    except ValueError as e:
        print(e, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
