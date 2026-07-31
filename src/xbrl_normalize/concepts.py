"""Canonical line items, the us-gaap tags that carry them, and how to derive
the ones filers leave out.

Tag order matters only when more than one candidate has a value for the *same*
period. Staleness is not handled here — it's handled by requiring an observation
for the requested period, which is what stops Microsoft's `Revenues` tag (last
used in 2010) from answering a question about 2026.
"""

from __future__ import annotations

from dataclasses import dataclass, field

INSTANT = "instant"
DURATION = "duration"


@dataclass(frozen=True)
class Line:
    key: str
    label: str
    kind: str          # instant (balance sheet) or duration (flow)
    tags: tuple[str, ...]
    unit: str = "USD"
    statement: str = ""


LINES: tuple[Line, ...] = (
    # ---- income statement -------------------------------------------------
    Line("revenue", "Revenue", DURATION, (
        # `Revenues` first: where a filer reports both, it is the wider total.
        # Walmart tags 713.2B here and 706.4B under the contract tag (net sales
        # only), and the total is the comparable figure.
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
    ), statement="income"),
    Line("cost_of_revenue", "Cost of revenue", DURATION, (
        "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices",
    ), statement="income"),
    Line("gross_profit", "Gross profit", DURATION, ("GrossProfit",), statement="income"),
    Line("operating_income", "Operating income", DURATION, (
        "OperatingIncomeLoss",
    ), statement="income"),
    Line("net_income", "Net income (to parent)", DURATION, (
        "NetIncomeLoss", "ProfitLoss",
    ), statement="income"),
    Line("net_income_incl_nci", "Net income incl. NCI", DURATION, (
        "ProfitLoss", "NetIncomeLoss",
    ), statement="income"),

    # ---- balance sheet ----------------------------------------------------
    Line("assets", "Total assets", INSTANT, ("Assets",), statement="balance"),
    Line("assets_current", "Current assets", INSTANT, ("AssetsCurrent",), statement="balance"),
    Line("liabilities", "Total liabilities", INSTANT, ("Liabilities",), statement="balance"),
    Line("liabilities_current", "Current liabilities", INSTANT, (
        "LiabilitiesCurrent",
    ), statement="balance"),
    # The accounting identity closes against equity *including* noncontrolling
    # interests. Exxon: 182.4 + 266.6 = 449.0 balances; 182.4 + 259.4 does not.
    Line("equity", "Total equity incl. NCI", INSTANT, (
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "StockholdersEquity",
    ), statement="balance"),
    Line("equity_parent", "Equity attributable to parent", INSTANT, (
        "StockholdersEquity",
    ), statement="balance"),
    # Mezzanine equity sits between liabilities and equity and is excluded from
    # both, so the identity does not close without it. It accounts for the gap
    # exactly at UnitedHealth (1.61B), Prudential (2.79B), Deere, MetLife and RTX.
    Line("temporary_equity", "Temporary (mezzanine) equity", INSTANT, (
        "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
        "RedeemableNoncontrollingInterestEquityCarryingAmount",
        "TemporaryEquityCarryingAmountAttributableToParent",
    ), statement="balance"),
    Line("liabilities_and_equity", "Liabilities and equity", INSTANT, (
        "LiabilitiesAndStockholdersEquity",
    ), statement="balance"),
    Line("cash", "Cash and equivalents", INSTANT, (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ), statement="balance"),

    # ---- cash flow --------------------------------------------------------
    Line("cfo", "Operating cash flow", DURATION, (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ), statement="cashflow"),
    Line("cfi", "Investing cash flow", DURATION, (
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    ), statement="cashflow"),
    Line("cff", "Financing cash flow", DURATION, (
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    ), statement="cashflow"),
    Line("capex", "Capital expenditure", DURATION, (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ), statement="cashflow"),
)

BY_KEY = {ln.key: ln for ln in LINES}


@dataclass(frozen=True)
class Derivation:
    target: str
    inputs: tuple[str, ...]
    op: str                                  # human-readable formula
    fn: object = field(repr=False, default=None)
    # Inputs that contribute 0 when absent. Most filers carry no mezzanine
    # equity, and requiring it would block the derivation for all of them.
    optional: tuple[str, ...] = ()


def _sub(a, b):
    return a - b


DERIVATIONS: tuple[Derivation, ...] = (
    # Walmart reports no current `Liabilities` tag at all; AT&T's went stale in
    # 2015. Both publish the combined total, so back it out — subtracting
    # mezzanine equity, which sits inside that total but belongs to neither
    # side. Omitting it double-counts and breaks the identity by ~0.1%.
    Derivation("liabilities", ("liabilities_and_equity", "equity"),
               "liabilities_and_equity - equity - temporary_equity",
               lambda a, b, t: a - b - t, optional=("temporary_equity",)),
    Derivation("equity", ("liabilities_and_equity", "liabilities"),
               "liabilities_and_equity - liabilities - temporary_equity",
               lambda a, b, t: a - b - t, optional=("temporary_equity",)),
    Derivation("gross_profit", ("revenue", "cost_of_revenue"),
               "revenue - cost_of_revenue", _sub),
    Derivation("assets", ("liabilities_and_equity",), "= liabilities_and_equity",
               lambda a: a),
)

DERIVATIONS_BY_TARGET: dict[str, list[Derivation]] = {}
for d in DERIVATIONS:
    DERIVATIONS_BY_TARGET.setdefault(d.target, []).append(d)
