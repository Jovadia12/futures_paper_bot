from datetime import date, datetime

import pytest

from v25bot.clock import NY
from v25bot.contracts import Contract, ContractError, front_contract, resolve, third_friday


def at(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def test_third_friday_expiries():
    assert third_friday(2026, 12) == date(2026, 12, 18)
    assert third_friday(2027, 3) == date(2027, 3, 19)
    assert third_friday(2026, 9) == date(2026, 9, 18)
    assert third_friday(2027, 6) == date(2027, 6, 18)


def test_contract_names_and_roll_date():
    z6 = Contract("MNQ", 12, 2026)
    assert z6.name == "MNQZ6"
    assert z6.tradingview_name == "MNQZ2026"
    assert z6.roll_date(8) == date(2026, 12, 10)  # Thursday of the prior week
    assert z6.next() == Contract("MNQ", 3, 2027)


@pytest.mark.parametrize("today,expected", [
    (date(2026, 10, 19), "MNQZ6"),
    (date(2026, 12, 9), "MNQZ6"),    # day before roll
    (date(2026, 12, 10), "MNQH7"),   # roll date
    (date(2026, 12, 25), "MNQH7"),
    (date(2027, 3, 11), "MNQM7"),    # H7 roll = 2027-03-11
    (date(2026, 1, 5), "MNQH6"),
])
def test_front_contract_rolls(today, expected):
    assert front_contract("MNQ", today, 8).name == expected


def test_second_continuous_contract():
    assert front_contract("MNQ", date(2026, 10, 19), 8, offset=1).name == "MNQH7"
    res = resolve("CME_MINI:MNQ2!", at(2026, 10, 19))
    assert res.contract.name == "MNQH7"


@pytest.mark.parametrize("symbol", ["CME_MINI:MNQ1!", "MNQ1!", "MNQ", "mnq1!"])
def test_continuous_aliases_resolve_to_front(symbol):
    res = resolve(symbol, at(2026, 10, 20))
    assert res.contract.name == "MNQZ6"
    assert res.continuous and res.source == "roll_schedule"


@pytest.mark.parametrize("symbol", ["MNQZ2026", "MNQZ26", "MNQZ6", "CME_MINI:MNQZ2026"])
def test_explicit_contract_formats(symbol):
    res = resolve(symbol, at(2026, 10, 20))
    assert res.contract.name == "MNQZ6" and not res.continuous


def test_single_digit_year_never_resolves_to_the_past_decade():
    assert resolve("MNQH7", at(2026, 10, 20)).contract.year == 2027


def test_expired_explicit_contract_rejected():
    with pytest.raises(ContractError, match="expired"):
        resolve("MNQU2026", at(2026, 10, 20))


def test_contract_too_close_to_expiry_rejected():
    with pytest.raises(ContractError, match="minimum"):
        resolve("MNQZ2026", at(2026, 12, 17), min_days_to_expiry=2)


def test_roll_window_without_chart_contract_is_refused():
    # 2026-12-14: after the Z6 roll date (12-10), before Z6 expiry (12-18).
    with pytest.raises(ContractError, match="roll window"):
        resolve("CME_MINI:MNQ1!", at(2026, 12, 14))


def test_roll_window_uses_chart_contract_when_supplied():
    res = resolve("CME_MINI:MNQ1!", at(2026, 12, 14), chart_contract="MNQH2027")
    assert res.contract.name == "MNQH7" and res.source == "chart_contract"


def test_chart_still_on_old_contract_is_traded_but_warned_and_expiry_guarded():
    # Before expiry minimum: chart says Z6 while schedule says H7 -> trade Z6 with warning.
    res = resolve("CME_MINI:MNQ1!", at(2026, 12, 11), chart_contract="MNQZ2026", min_days_to_expiry=2)
    assert res.contract.name == "MNQZ6" and res.warnings
    # Inside the minimum: refuse rather than trade an expiring month.
    with pytest.raises(ContractError, match="switch the chart to MNQH2027"):
        resolve("CME_MINI:MNQ1!", at(2026, 12, 17), chart_contract="MNQZ2026", min_days_to_expiry=2)


def test_after_expiry_continuous_moves_to_next_contract():
    assert resolve("MNQ1!", at(2026, 12, 21)).contract.name == "MNQH7"


@pytest.mark.parametrize("symbol", ["NQ1!", "CME_MINI:ES1!", "MNQX6", "MNQ1", "AAPL", ""])
def test_unsupported_symbols_rejected(symbol):
    with pytest.raises(ContractError):
        resolve(symbol, at(2026, 10, 20))


def test_chart_contract_root_mismatch_rejected():
    with pytest.raises(ContractError):
        resolve("CME_MINI:MNQ1!", at(2026, 10, 20), chart_contract="NQZ2026")
