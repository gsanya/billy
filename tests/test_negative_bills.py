"""Negative bill amounts: credit notes and refunds (e.g. annual settlement bills)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import pytest

from test_bill_placeholders import configure, manager, run  # noqa: F401  (pytest fixture)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "bill_tracker"))

from parser.engine import ParserEngine, ParserError  # noqa: E402


def add(manager, **kwargs):
    return run(manager.async_add(
        year=2026, month=kwargs.pop("month", 10), category_id="electricity",
        category_name=None, amount=kwargs.pop("amount", 80), **kwargs,
    ))


def with_payers(manager):
    manager.payers = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
    return [{"payer_id": "a", "percentage": 50}, {"payer_id": "b", "percentage": 50}]


def test_bills_accept_negative_amounts_and_totals_subtract_them(manager):
    add(manager, amount=100, paid=True)
    credit = add(manager, amount=-30.5, paid=True)
    assert credit["amount"] == -30.5
    october = next(row for row in manager.monthly_totals() if row["key"] == "2026-10")
    assert october["total"] == 69.5

    updated = run(manager.async_update(
        credit["id"], year=2026, month=10, category_id="electricity",
        category_name=None, amount=-12,
    ))
    assert updated["amount"] == -12


def test_negative_bills_survive_reload(manager):
    credit = add(manager, amount=-25)
    manager._migrate_expenses()
    assert [x["amount"] for x in manager.expenses if x["id"] == credit["id"]] == [-25]


@pytest.mark.parametrize("amount", [float("nan"), float("inf")])
def test_non_finite_amounts_are_still_rejected(manager, amount):
    with pytest.raises(ValueError):
        add(manager, amount=amount)
    assert manager.expenses == []


def test_other_amounts_stay_positive(manager):
    with pytest.raises(ValueError):
        manager._validate_amount(-1)
    with pytest.raises(ValueError):
        manager._validate_amount(-1, allow_zero=False)
    with pytest.raises(ValueError):
        manager._validate_amount(0, allow_zero=False)
    manager._validate_amount(-1, allow_negative=True)


def test_credit_reverses_the_reimbursement_direction(manager):
    split = with_payers(manager)
    add(manager, amount=-40, payer_id="a", split=split)
    debts = manager.debts()
    assert len(debts) == 1
    assert (debts[0]["from_payer_id"], debts[0]["to_payer_id"], debts[0]["amount"]) == ("a", "b", 20)


def test_credit_is_netted_against_a_normal_bill(manager):
    split = with_payers(manager)
    add(manager, amount=100, payer_id="a", split=split)
    add(manager, amount=-40, payer_id="a", split=split)
    debts = manager.debts()
    assert len(debts) == 1
    assert (debts[0]["from_payer_id"], debts[0]["to_payer_id"], debts[0]["amount"]) == ("b", "a", 30)


def test_credit_reimbursement_can_be_settled(manager):
    split = with_payers(manager)
    credit = add(manager, amount=-40, payer_id="a", split=split)
    run(manager.async_add_settlement(from_payer_id="a", to_payer_id="b", amount=20))
    assert manager.debts() == []
    public = next(x for x in manager.expenses if x["id"] == credit["id"])
    assert manager._expense_reimbursement_state(public)["status"] == "done"


def test_estimates_and_placeholders_ignore_credits(manager):
    add(manager, month=8, amount=80)
    add(manager, month=9, amount=-50)
    _, estimate_history = manager._forecast_category_history(manager.categories[0])
    assert manager._estimate_category_amount(estimate_history) == 80
    configure(manager, start_month="2026-10")
    assert manager._sync_bill_placeholders()
    placeholder = next(x for x in manager.expenses if x.get("is_placeholder"))
    assert placeholder["amount"] == 80


def test_paid_credit_increases_the_account_balance(manager):
    manager.payers = [{"id": "a", "name": "A"}]
    bank = run(manager.async_add_account(
        name="Bank", account_type="checking", opening_balance=1000,
        opening_date="2026-01-01", payer_id="a",
    ))
    add(manager, amount=100, paid=True, payment_date="2026-10-05", account_id=bank["id"])
    add(manager, amount=-30, paid=True, payment_date="2026-10-06", account_id=bank["id"])
    balances = manager.account_balances(date(2026, 10, 31))
    assert balances["accounts"][0]["balance"] == 930


def test_csv_import_keeps_negative_amounts(manager):
    assert manager.exporter.parse_csv_amount("-1.234,50") == -1234.5
    assert manager.exporter.parse_csv_amount("-12") == -12


@pytest.mark.parametrize("raw, expected", [
    ("-1 234", -1234.0),
    ("−1 234", -1234.0),
    ("1 234-", -1234.0),
    ("1 234", 1234.0),
])
def test_parser_reads_negative_amounts(raw, expected):
    assert ParserEngine()._decimal(raw, "hu_HU") == expected


def test_parser_accepts_credits_within_range():
    engine = ParserEngine()
    engine._validate_result({"amount": -5000})
    with pytest.raises(ParserError):
        engine._validate_result({"amount": -2_000_000})
