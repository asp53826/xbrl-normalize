# Design notes

Every non-obvious decision here came from running the pipeline over a
cross-sector sample and looking at what broke. The accounting identity is the
reason that worked: it is never used as an input to normalisation, so when
`assets ≠ liabilities + equity` it is genuinely reporting a defect rather than
restating an assumption.

## Why "first tag that exists" is wrong

The obvious implementation keeps an ordered list of candidate tags per line item
and takes the first one present in the company's facts. It produces confidently
wrong numbers.

Microsoft's facts contain `Revenues`. They also contain
`RevenueFromContractWithCustomerExcludingAssessedTax`. Presence-order resolution
picks `Revenues` and returns **62.5B** for FY2026 — the value from **2010**,
which is when Microsoft last used that tag. The correct answer is 331.8B.

Exxon is the mirror image: `Revenues` is current at 332.2B while the contract
tag went stale in 2021. There is no fixed priority that is right for both.

The fix is that resolution is period-scoped, not existence-scoped: a tag only
answers if it has an observation whose period matches the one being asked about.
Tag order then only breaks ties between candidates that *both* cover the period.
Walmart is the case where that happens — 713.2B under `Revenues` (total revenue)
versus 706.4B under the contract tag (net sales only) — and `Revenues` is
ordered first because the wider total is the comparable figure.

Across 102 companies the winning tag splits 55 / 37 / 4 across three tags. No
single tag covers even 55% of large caps.

## The identity only closes with equity including NCI

`StockholdersEquity` is the intuitive pick for "equity". It is the wrong one for
a balance check, because `LiabilitiesAndStockholdersEquity` includes
noncontrolling interests and `StockholdersEquity` does not.

Exxon, FY2025, in billions:

```
Liabilities                      182.4
StockholdersEquity               259.4   ->  441.8   ≠ Assets
StockholdersEquity + NCI         266.6   ->  449.0   =  Assets 449.0
```

So `equity` resolves `...IncludingPortionAttributableToNoncontrollingInterest`
first and falls back to `StockholdersEquity`, which is correct for the many
filers with no NCI at all — for them the two are the same number. Equity
attributable to parent is kept as a separate line for anyone who wants it.

## Mezzanine equity, and the double-count it caused

With equity fixed, seven companies still failed the identity. Five of them had
gaps explained *exactly* by a single tag:

```
UNH  gap 1.61B  = RedeemableNoncontrollingInterestEquityCarryingAmount 1.61B
PRU  gap 2.79B  = 2.79B
DE   gap 0.05B  = 0.05B
MET  gap 0.24B  = 0.24B
RTX  gap 0.04B  = 0.04B
```

Redeemable noncontrolling interests are *temporary equity*: contractually
redeemable, so not permanent equity, but not a liability either. They sit
between the two sections and are excluded from both totals. The identity is
really `assets = liabilities + temporary equity + equity`.

Adding that line fixed those five and immediately broke four others — AT&T,
Thermo Fisher, Walmart and Comcast went from exact to off by ~0.1%. All four
have **derived** liabilities. The derivation is
`liabilities_and_equity - equity`, and that residual already contains the
mezzanine, so adding it again on the identity side double-counts it.

The derivation had to become `liabilities_and_equity - equity - temporary_equity`,
which required derivation inputs that contribute zero when absent — most filers
have no mezzanine equity and requiring it would block the derivation for
everyone.

Net effect on the identity, exact matches: 91.1% → 92.2% → **96.1%**.

## What is left unexplained

BlackRock is off by 3.19% (5.43B) and AEP by 0.03%. Scanning every instant tag
in their own filings for a value matching the gap finds nothing — no single
concept accounts for it. Their tagged `Liabilities` and equity simply do not sum
to their own tagged total.

Two options: derive liabilities from the total whenever the direct tag
disagrees, forcing every balance sheet to close; or report the residual. The
second is what this does. A number that always balances because it was
constructed to balance is not evidence of anything, and silently overriding a
filer's own tagged value hides exactly the data-quality signal someone using
this would want.

## Derivation and the recursion it invites

`liabilities` derives from `equity`, and `equity` derives from `liabilities`.
Both are legitimate — filers omit either one — but together they recurse
forever. The resolver tracks in-progress keys and returns `None` on re-entry, so
a company missing both simply reports both as missing rather than exhausting the
stack. There's a test pinning that.

Resolution is memoised per period-kind. Instants (balance sheet) and durations
(flows) get separate resolvers, since a balance-sheet line is read at a point in
time and a flow line over a span, and the same key must not be shared between
them.

## Fiscal periods

Period discovery does not anchor on a designated tag, because no single tag is
present for every filer. It collects every duration of annual length (330–400
days) across all tags and keeps the most-attested span per end date. That
handles 52/53-week retail calendars — Apple's FY2025 runs 2024-09-29 to
2025-09-27, and Walmart's ends 2026-01-31 — without special-casing anything.

Balance-sheet instants are then read at the chosen duration's end date.

Quarterly periods are detected but excluded. Q4 is rarely tagged directly and
would have to be derived as the full year minus Q1–Q3, which is a different
piece of work with its own failure modes.

## Ticker resolution follows the live registrant

`XOM` in SEC's ticker file maps to CIK **2115436**, not the 34088 that carries
Exxon's filing history. The company reorganised under a new holding-company
registrant and the new CIK's companyfacts is essentially empty.

The first benchmark run reported this as "18 of 18 lines missing", which looks
like a mapping bug and is not one. `normalize` now raises `NoAnnualData` naming
the CIK and suggesting the predecessor, because a registrant with no history is
a categorically different situation from one whose lines failed to resolve.
There is no reliable programmatic link from a successor CIK to its predecessor,
so the caller has to supply it.

## Rate limiting

Same pacer as [edgar-mcp](https://github.com/asp53826/edgar-mcp), for the same
reason: a token bucket seeded full releases `capacity` requests instantly and
then sustains the rate on top, so a burst of concurrent fetches can put roughly
double the limit inside one second. The benchmark issues 102 concurrent
companyfacts requests, which is exactly that shape. Grants are spaced `1/rate`
apart at 9 req/s against SEC's stated ceiling of 10.
