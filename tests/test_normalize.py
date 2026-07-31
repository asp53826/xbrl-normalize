import pytest

from xbrl_normalize.normalize import Facts, NoAnnualData, normalize

USD = "USD"


def fact(val, end, start=None, filed="2026-01-01", form="10-K", fp="FY"):
    d = {"val": val, "end": end, "filed": filed, "form": form, "fp": fp}
    if start:
        d["start"] = start
    return d


def company(tags: dict, name="TESTCO", cik=1):
    return {
        "cik": cik,
        "entityName": name,
        "facts": {"us-gaap": {t: {"units": {USD: pts}} for t, pts in tags.items()}},
    }


FY25 = ("2025-01-01", "2025-12-31")
FY24 = ("2024-01-01", "2024-12-31")


def balanced(**over):
    """A filer whose books balance, for tests that vary one thing."""
    tags = {
        "Revenues": [fact(1000, FY25[1], FY25[0]), fact(900, FY24[1], FY24[0])],
        "NetIncomeLoss": [fact(100, FY25[1], FY25[0])],
        "Assets": [fact(5000, FY25[1])],
        "Liabilities": [fact(3000, FY25[1])],
        "StockholdersEquity": [fact(2000, FY25[1])],
        "LiabilitiesAndStockholdersEquity": [fact(5000, FY25[1])],
    }
    tags.update(over)
    return company(tags)


# --------------------------------------------------------------------------
# the three problems this library exists to solve
# --------------------------------------------------------------------------

def test_stale_tag_does_not_answer_for_a_period_it_never_covered():
    """Microsoft's `Revenues` last carried a value in 2010 while the company
    kept filing. Presence of a tag must not imply it answers for FY2025."""
    st = normalize(balanced(**{
        "Revenues": [fact(62_500, "2010-06-30", "2009-07-01")],
        "RevenueFromContractWithCustomerExcludingAssessedTax": [
            fact(331_800, FY25[1], FY25[0])
        ],
    }))
    assert st.get("revenue") == 331_800
    assert st.values["revenue"].source == "RevenueFromContractWithCustomerExcludingAssessedTax"


def test_restatement_takes_the_most_recent_filing():
    st = normalize(balanced(**{
        "Revenues": [
            fact(1000, FY25[1], FY25[0], filed="2026-02-01"),
            fact(1050, FY25[1], FY25[0], filed="2027-02-01"),  # restated later
        ],
    }))
    assert st.get("revenue") == 1050


def test_wider_total_wins_when_two_tags_cover_the_same_period():
    """Walmart tags 713.2B under Revenues and 706.4B under the contract tag."""
    st = normalize(balanced(**{
        "Revenues": [fact(713_200, FY25[1], FY25[0])],
        "RevenueFromContractWithCustomerExcludingAssessedTax": [
            fact(706_400, FY25[1], FY25[0])
        ],
    }))
    assert st.get("revenue") == 713_200


# --------------------------------------------------------------------------
# derivation
# --------------------------------------------------------------------------

def test_liabilities_derived_when_the_filer_omits_the_tag():
    """Walmart publishes no current `Liabilities` tag."""
    tags = dict(balanced()["facts"]["us-gaap"])
    st = normalize(company({k: v["units"][USD] for k, v in tags.items()
                            if k != "Liabilities"}))
    v = st.values["liabilities"]
    assert v.value == 3000 and v.method == "derived"
    assert st.identity_error() < 1e-12


def test_equity_derived_when_the_filer_omits_the_tag():
    tags = {k: v["units"][USD] for k, v in balanced()["facts"]["us-gaap"].items()
            if k != "StockholdersEquity"}
    st = normalize(company(tags))
    assert st.values["equity"].method == "derived"
    assert st.get("equity") == 2000


def test_liabilities_and_equity_do_not_recurse_forever():
    """Each derives from the other; without a guard this blows the stack."""
    tags = {k: v["units"][USD] for k, v in balanced()["facts"]["us-gaap"].items()
            if k not in ("Liabilities", "StockholdersEquity")}
    st = normalize(company(tags))
    assert "liabilities" in st.missing and "equity" in st.missing


def test_gross_profit_derived_from_revenue_and_cost():
    st = normalize(balanced(**{"CostOfRevenue": [fact(600, FY25[1], FY25[0])]}))
    assert st.get("gross_profit") == 400
    assert st.values["gross_profit"].method == "derived"


# --------------------------------------------------------------------------
# mezzanine equity
# --------------------------------------------------------------------------

def test_identity_accounts_for_mezzanine_equity():
    """Redeemable NCI belongs to neither side; without it UnitedHealth and
    Prudential look broken by 0.5% and 0.36%."""
    st = normalize(company({
        "Assets": [fact(5000, FY25[1])],
        "Liabilities": [fact(2900, FY25[1])],
        "StockholdersEquity": [fact(2000, FY25[1])],
        "RedeemableNoncontrollingInterestEquityCarryingAmount": [fact(100, FY25[1])],
        "LiabilitiesAndStockholdersEquity": [fact(5000, FY25[1])],
        "Revenues": [fact(1, FY25[1], FY25[0])],
    }))
    assert st.get("temporary_equity") == 100
    assert st.identity_error() < 1e-12


def test_derived_liabilities_do_not_double_count_mezzanine():
    """The residual already contains mezzanine equity. Adding it back on top
    is the bug that broke AT&T, Thermo Fisher, Walmart and Comcast."""
    st = normalize(company({
        "Assets": [fact(5000, FY25[1])],
        "StockholdersEquity": [fact(2000, FY25[1])],
        "RedeemableNoncontrollingInterestEquityCarryingAmount": [fact(100, FY25[1])],
        "LiabilitiesAndStockholdersEquity": [fact(5000, FY25[1])],
        "Revenues": [fact(1, FY25[1], FY25[0])],
    }))
    assert st.values["liabilities"].method == "derived"
    assert st.get("liabilities") == 2900
    assert st.identity_error() < 1e-12


def test_mezzanine_absent_is_treated_as_zero():
    st = normalize(balanced())
    assert "temporary_equity" in st.missing
    assert st.identity_error() < 1e-12


# --------------------------------------------------------------------------
# periods
# --------------------------------------------------------------------------

def test_instants_are_read_at_the_duration_end():
    st = normalize(balanced(**{
        "Assets": [fact(5000, FY25[1]), fact(4000, FY24[1])],
    }))
    assert st.get("assets") == 5000


def test_quarterly_durations_are_not_mistaken_for_annual():
    st = normalize(balanced(**{
        "Revenues": [
            fact(250, "2025-12-31", "2025-10-01"),   # Q4 only
            fact(1000, "2025-12-31", "2025-01-01"),  # full year
        ],
    }))
    assert st.get("revenue") == 1000


def test_52_53_week_calendars_pick_the_most_attested_span():
    f = Facts(company({
        "Revenues": [fact(1000, "2025-09-27", "2024-09-29")],
        "CostOfRevenue": [fact(600, "2025-09-27", "2024-09-29")],
        "Assets": [fact(5000, "2025-09-27")],
    }))
    periods = f.annual_periods()
    assert periods[0].end == "2025-09-27"
    assert periods[0].start == "2024-09-29"


def test_explicit_fiscal_year_selection():
    st = normalize(balanced(), fy=2024)
    assert st.period.fy == 2024 and st.get("revenue") == 900


def test_unknown_fiscal_year_lists_what_is_available():
    with pytest.raises(ValueError, match="available"):
        normalize(balanced(), fy=1999)


def test_registrant_with_no_annual_history_says_so():
    """A ticker resolves to the current registrant, which after a reorg can be
    a fresh CIK with an empty companyfacts — Exxon's XOM points at CIK 2115436."""
    with pytest.raises(NoAnnualData, match="no annual XBRL data"):
        normalize(company({}, name="NEWCO", cik=2115436))


def test_missing_lines_are_reported_not_silently_zero():
    st = normalize(balanced())
    assert "capex" in st.missing
    assert st.get("capex") is None
