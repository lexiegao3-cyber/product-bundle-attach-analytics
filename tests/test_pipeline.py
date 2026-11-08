"""Checks on baskets and rules. Run: python -m pytest -q"""
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "warehouse" / "basket.sqlite"
T = ROOT / "reports" / "tables"


@pytest.fixture(scope="module")
def con():
    if not DB.exists():
        pytest.skip("run `python run_all.py` first (needs data/raw/online_retail_II.xlsx)")
    with sqlite3.connect(DB) as c:
        yield c


def test_basket_has_no_cancellations_or_fees(con):
    d = pd.read_sql("SELECT invoice, stock_code, quantity FROM basket_line", con)
    assert not d["invoice"].str.startswith(("C", "A")).any()
    assert d["stock_code"].str.match(r"^(\d{5}[A-Za-z]{0,2}|DCGS\w+)$").all()
    assert (d["quantity"] > 0).all()


def test_one_row_per_invoice_product(con):
    d = pd.read_sql("SELECT invoice, stock_code FROM basket_line", con)
    assert not d.duplicated().any()


def test_orders_match_lines(con):
    a = pd.read_sql("SELECT COUNT(DISTINCT invoice) n FROM basket_line", con).n[0]
    b = pd.read_sql("SELECT COUNT(*) n FROM orders", con).n[0]
    assert a == b


def test_rule_metrics_consistent():
    r = pd.read_csv(T / "association_rules_all.csv")
    assert np.allclose(r["attach_rate"], r["pair_orders"] / r["anchor_orders"])
    assert np.allclose(r["lift"], r["attach_rate"] / (r["companion_orders"] / r["n_orders"]))
    assert (r["pair_orders"] >= 150).all()
    assert (r["attach_rate"] <= 1).all()


def test_rules_symmetric_support():
    r = pd.read_csv(T / "association_rules_all.csv")
    fwd = r.set_index(["anchor", "companion"])["pair_orders"]
    rev = r.set_index(["companion", "anchor"])["pair_orders"]
    rev.index.names = fwd.index.names
    assert fwd.sort_index().equals(rev.sort_index())


def test_rfm_covers_all_identified_customers(con):
    n = pd.read_sql("SELECT COUNT(DISTINCT customer_id) n FROM orders WHERE is_guest = 0", con).n[0]
    seg = pd.read_csv(T / "rfm_segments.csv")
    assert seg["customers"].sum() == n
