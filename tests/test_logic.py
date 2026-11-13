"""Pure logic tests: CSV validation, price rules, reconciliation classifiers."""
from __future__ import annotations

import textwrap
from decimal import Decimal

import pytest

from terminal_assistant.csvplan import PlanError, load_changes
from terminal_assistant.reconcile import (
    classify_post_disconnect,
    classify_post_save,
    price,
    values_equal,
)

PRE = {"name": "铅笔", "unit": "支", "price": "1.20", "revision": 1}
PLAN = {"name": "中华铅笔", "unit": "支", "price": "1.50"}


def write_csv(tmp_path, body: str):
    path = tmp_path / "changes.csv"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return str(path)


def test_load_valid_changes(tmp_path):
    path = write_csv(
        tmp_path,
        """
        catalog_code,name,unit,price,expected_old_name
        A001,中华铅笔,支,1.50,铅笔
        A002,绿茶包,盒,20.0,茶包
        """,
    )
    rows, digest = load_changes(path)
    assert [r.code for r in rows] == ["A001", "A002"]
    assert rows[0].price == Decimal("1.50")
    assert rows[1].price_text == "20.00"
    assert len(digest) == 64


@pytest.mark.parametrize(
    "price_text,message",
    [
        ("1.505", "最多两位小数"),
        ("-1", "非负十进制"),
        ("abc", "非负十进制"),
        ("", "价格不能为空"),
        ("1.5e3", "非负十进制"),
        ("1.", "非负十进制"),
        ("+1.50", "非负十进制"),
    ],
)
def test_price_rejections(tmp_path, price_text, message):
    path = write_csv(
        tmp_path,
        f"""
        catalog_code,name,unit,price,expected_old_name
        A001,中华铅笔,支,{price_text},铅笔
        """,
    )
    with pytest.raises(PlanError) as exc:
        load_changes(path)
    assert any(message in e for e in exc.value.errors)


def test_control_chars_and_duplicates(tmp_path):
    path = write_csv(
        tmp_path,
        """
        catalog_code,name,unit,price,expected_old_name
        A001,中华铅笔,支,1.50,铅笔
        A001,又一个,支,2.00,铅笔
        A003,\x01балл,支,1.00,米
        """,
    )
    with pytest.raises(PlanError) as exc:
        load_changes(path)
    joined = " ".join(exc.value.errors)
    assert "重复" in joined


def test_unknown_columns_rejected(tmp_path):
    path = write_csv(
        tmp_path,
        """
        catalog_code,name,unit,price,expected_old_name,extra
        A001,中华铅笔,支,1.50,铅笔,x
        """,
    )
    with pytest.raises(PlanError) as exc:
        load_changes(path)
    assert any("未知列" in e for e in exc.value.errors)


def test_missing_required_column(tmp_path):
    path = write_csv(
        tmp_path,
        """
        catalog_code,name,unit,price
        A001,中华铅笔,支,1.50
        """,
    )
    with pytest.raises(PlanError) as exc:
        load_changes(path)
    assert any("expected_old_name" in e for e in exc.value.errors)


@pytest.mark.parametrize("empty", ["", "catalog_code,name,unit,price,expected_old_name\n"])
def test_empty_csv_rejected(tmp_path, empty):
    path = tmp_path / "changes.csv"
    path.write_text(empty, encoding="utf-8")
    with pytest.raises(PlanError):
        load_changes(str(path))


def test_price_helper():
    assert price("1.20") == Decimal("1.2")
    assert price(" 2 ") == Decimal("2")
    assert price("abc") is None
    assert price(None) is None


def test_values_equal_price_semantics():
    assert values_equal({"name": "a", "unit": "支", "price": "1.2"}, {"name": "a", "unit": "支", "price": "1.20"})
    assert not values_equal({"name": "a", "unit": "支", "price": "1.2"}, {"name": "b", "unit": "支", "price": "1.20"})
    assert not values_equal(None, PLAN)


def test_post_save_confirmed_requires_revision_increment():
    status, _ = classify_post_save(PRE, PLAN, {"name": "中华铅笔", "unit": "支", "price": "1.50"}, 2)
    assert status == "confirmed"
    status, _ = classify_post_save(PRE, PLAN, {"name": "中华铅笔", "unit": "支", "price": "1.5"}, 2)
    assert status == "confirmed"  # decimal equality
    status, _ = classify_post_save(PRE, PLAN, {"name": "中华铅笔", "unit": "支", "price": "1.50"}, 1)
    assert status == "readback_revision_anomaly"
    status, _ = classify_post_save(PRE, PLAN, {"name": "铅笔", "unit": "支", "price": "1.20"}, 2)
    assert status == "readback_mismatch"


def test_post_disconnect_current_confirmed():
    status, detail = classify_post_disconnect(
        PRE, PLAN, {"name": "中华铅笔", "unit": "支", "price": "1.50"}, 2
    )
    assert status == "confirmed_current"
    assert "不证明恰好一次写入" in detail


def test_post_disconnect_old_unchanged():
    status, _ = classify_post_disconnect(PRE, PLAN, {"name": "铅笔", "unit": "支", "price": "1.20"}, 1)
    assert status == "old_unchanged"


def test_post_disconnect_old_values_but_revision_bumped_is_conflict():
    status, _ = classify_post_disconnect(PRE, PLAN, {"name": "铅笔", "unit": "支", "price": "1.20"}, 2)
    assert status == "conflict_after_disconnect"


def test_post_disconnect_partial_is_conflict():
    status, _ = classify_post_disconnect(
        PRE, PLAN, {"name": "中华铅笔", "unit": "支", "price": "1.30"}, 2
    )
    assert status == "conflict_after_disconnect"


def test_post_disconnect_unreadable_is_conflict():
    status, _ = classify_post_disconnect(PRE, PLAN, {"name": None, "unit": None, "price": None}, 2)
    assert status == "conflict_after_disconnect"
