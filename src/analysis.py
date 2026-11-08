"""
Bundle & attach-rate analysis + customer value segmentation.

  1. Product association rules from the SQL Apriori query (sql/basket_rules.sql),
     cross-checked with an independent sparse-matrix count in Python.
  2. Stability check: does each rule hold in BOTH 2010 and 2011 baskets?
  3. Attach-rate table per anchor product and the size of the "missed attach" gap.
  4. RFM segmentation and 12-month customer value by acquisition cohort.
  5. Outputs: reports/tables/*.csv, charts, excel/bundle_explorer.xlsx, reports/findings.md
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo
from scipy import sparse
import string


def nice(text: str) -> str:
    return string.capwords(str(text).lower())

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "warehouse" / "basket.sqlite"
SQL = ROOT / "sql" / "basket_rules.sql"
TABLES = ROOT / "reports" / "tables"
FIGS = ROOT / "reports" / "figures"
XLSX = ROOT / "excel" / "bundle_explorer.xlsx"

MIN_ITEM_ORDERS = 300      # product in >= ~0.8% of multi-product orders
MIN_PAIR_ORDERS = 150      # pair in >= ~0.4% of multi-product orders
STRONG_CONF, STRONG_LIFT = 0.30, 3.0

BLUE, GREY, INK, INK2, GRID, SURFACE = "#2a78d6", "#b7b6b0", "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"


def sql_blocks() -> dict[str, str]:
    return {b.partition("\n")[0].strip(): b.partition("\n")[2]
            for b in re.split(r"^-- name: ", SQL.read_text(), flags=re.M)[1:]}


# ------------------------------------------------------------------ pair counting in Python
def pair_counts_sparse(lines: pd.DataFrame, min_item: int, min_pair: int) -> pd.DataFrame:
    """Independent implementation: order x product incidence matrix, X^T X gives co-occurrence."""
    inv = lines["invoice"].astype("category")
    sku = lines["stock_code"].astype("category")
    X = sparse.csr_matrix((np.ones(len(lines), dtype=np.int32), (inv.cat.codes, sku.cat.codes)))
    item = np.asarray(X.sum(axis=0)).ravel()
    keep = np.where(item >= min_item)[0]
    Xk = X[:, keep]
    C = (Xk.T @ Xk).tocoo()
    m = (C.row < C.col) & (C.data >= min_pair)
    codes = np.asarray(sku.cat.categories)[keep]
    out = pd.DataFrame({"a": codes[C.row[m]], "b": codes[C.col[m]], "pair_orders": C.data[m]})
    swap = out["a"] > out["b"]
    out.loc[swap, ["a", "b"]] = out.loc[swap, ["b", "a"]].values
    return out.rename(columns={"a": "item_a", "b": "item_b"})


def rules_from_pairs(p: pd.DataFrame, products: pd.DataFrame) -> pd.DataFrame:
    """Expand each pair into two directional rules with support / confidence / lift."""
    a = p.rename(columns={"item_a": "anchor", "item_b": "companion",
                          "item_a_orders": "anchor_orders", "item_b_orders": "companion_orders"})
    b = p.rename(columns={"item_b": "anchor", "item_a": "companion",
                          "item_b_orders": "anchor_orders", "item_a_orders": "companion_orders"})
    r = pd.concat([a, b], ignore_index=True)
    r["support"] = r["pair_orders"] / r["n_orders"]
    r["attach_rate"] = r["pair_orders"] / r["anchor_orders"]          # = confidence
    r["lift"] = r["attach_rate"] / (r["companion_orders"] / r["n_orders"])
    r["orders_anchor_without_companion"] = r["anchor_orders"] - r["pair_orders"]
    d = products.set_index("stock_code")
    r["anchor_desc"] = r["anchor"].map(d["description"])
    r["companion_desc"] = r["companion"].map(d["description"])
    r["anchor_category"] = r["anchor"].map(d["category"])
    r["companion_category"] = r["companion"].map(d["category"])
    r["companion_avg_price"] = r["companion"].map(d["avg_unit_price"])
    cols = ["anchor", "anchor_desc", "anchor_category", "companion", "companion_desc", "companion_category",
            "pair_orders", "anchor_orders", "companion_orders", "n_orders", "support", "attach_rate", "lift",
            "orders_anchor_without_companion", "companion_avg_price"]
    return r[cols].sort_values(["anchor", "attach_rate"], ascending=[True, False]).reset_index(drop=True)


def yearly_lift(lines: pd.DataFrame, orders: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    """Recompute lift for each rule separately on 2010 and 2011 baskets."""
    multi = orders.loc[orders["distinct_products"] >= 2, ["invoice", "invoice_date"]]
    lines = lines.merge(multi, on="invoice", suffixes=("", "_o"))
    out = {}
    for yr in (2010, 2011):
        ly = lines[pd.to_datetime(lines["invoice_date"]).dt.year == yr]
        n = ly["invoice"].nunique()
        item = ly.groupby("stock_code")["invoice"].nunique()
        need = set(rules["anchor"]) | set(rules["companion"])
        ly = ly[ly["stock_code"].isin(need)]
        pc = pair_counts_sparse(ly, 1, 1)
        pc = pd.concat([pc, pc.rename(columns={"item_a": "item_b", "item_b": "item_a"})])
        key = pc.set_index(["item_a", "item_b"])["pair_orders"]
        idx = list(zip(rules["anchor"], rules["companion"]))
        pair = key.reindex(idx).fillna(0).to_numpy()
        conf = pair / rules["anchor"].map(item).to_numpy()
        out[f"lift_{yr}"] = conf / (rules["companion"].map(item).to_numpy() / n)
        out[f"pair_orders_{yr}"] = pair.astype(int)
    return pd.DataFrame(out, index=rules.index)


# ------------------------------------------------------------------ customers
def rfm(orders: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    o = orders[orders["is_guest"] == 0].copy()
    o["invoice_date"] = pd.to_datetime(o["invoice_date"])
    snap = o["invoice_date"].max() + pd.Timedelta(days=1)
    c = o.groupby("customer_id").agg(last=("invoice_date", "max"), frequency=("invoice", "nunique"),
                                     monetary=("order_value", "sum"), first=("invoice_date", "min"))
    c["recency_days"] = (snap - c["last"]).dt.days
    c["R"] = pd.qcut(c["recency_days"].rank(method="first"), 5, labels=[5, 4, 3, 2, 1]).astype(int)
    c["F"] = pd.qcut(c["frequency"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(int)
    c["M"] = pd.qcut(c["monetary"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(int)

    def seg(r):
        if r.R >= 4 and r.F >= 4:
            return "Champions"
        if r.F >= 4:
            return "Loyal - slipping" if r.R <= 2 else "Loyal"
        if r.R >= 4 and r.F <= 2:
            return "New / promising"
        if r.R <= 2 and r.F <= 2:
            return "Lapsed"
        return "Needs attention"
    c["segment"] = c.apply(seg, axis=1)
    summary = (c.groupby("segment")
                .agg(customers=("frequency", "size"), avg_recency_days=("recency_days", "mean"),
                     avg_orders=("frequency", "mean"), revenue=("monetary", "sum"))
                .assign(share_of_customers=lambda d: d.customers / d.customers.sum(),
                        share_of_revenue=lambda d: d.revenue / d.revenue.sum())
                .sort_values("revenue", ascending=False).round(3).reset_index())
    return c.reset_index(), summary


def value_12m(orders: pd.DataFrame) -> pd.DataFrame:
    """Merchandise spend in the 365 days after first purchase, for cohorts with a full year of data."""
    o = orders[orders["is_guest"] == 0].copy()
    o["invoice_date"] = pd.to_datetime(o["invoice_date"])
    first = o.groupby("customer_id")["invoice_date"].min().rename("first")
    o = o.join(first, on="customer_id")
    end = o["invoice_date"].max()
    o = o[o["first"] <= end - pd.Timedelta(days=365)]
    w = o[o["invoice_date"] < o["first"] + pd.Timedelta(days=365)]
    cust = w.groupby("customer_id").agg(first=("first", "min"), value_12m=("order_value", "sum"),
                                        orders_12m=("invoice", "nunique"))
    cust["cohort_quarter"] = cust["first"].dt.to_period("Q").astype(str)
    cust.loc[cust["first"] < "2010-01-01", "cohort_quarter"] = "2009-12 (existing base)"
    return (cust.groupby("cohort_quarter")
                .agg(customers=("value_12m", "size"), avg_value_12m=("value_12m", "mean"),
                     median_value_12m=("value_12m", "median"), avg_orders_12m=("orders_12m", "mean"),
                     repeat_rate_12m=("orders_12m", lambda s: (s >= 2).mean()))
                .round(2).reset_index())


# ------------------------------------------------------------------ charts
def style(ax, title, xlabel=None, ylabel=None):
    ax.set_facecolor(SURFACE); ax.figure.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=12)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.set_axisbelow(True)
    if xlabel:
        ax.set_xlabel(xlabel, color=INK2, fontsize=9)
    if ylabel:
        ax.set_ylabel(ylabel, color=INK2, fontsize=9)


def charts(top: pd.DataFrame, rfm_sum: pd.DataFrame) -> None:
    FIGS.mkdir(parents=True, exist_ok=True)
    t = top.head(12).iloc[::-1]
    labels = [f"{nice(a)[:30]}  ->  {nice(c)[:30]}" for a, c in zip(t["anchor_desc"], t["companion_desc"])]
    fig, ax = plt.subplots(figsize=(10, 5.2))
    ax.barh(labels, t["attach_rate"] * 100, color=BLUE, height=0.6)
    style(ax, "Strongest bundles: % of anchor orders that also contain the companion", "% attach rate")
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    for y, (v, l) in enumerate(zip(t["attach_rate"] * 100, t["lift"])):
        ax.text(v + 0.8, y, f"{v:.0f}%  (lift {l:.0f}x)", va="center", fontsize=8, color=INK2)
    ax.set_xlim(0, 112)
    fig.tight_layout(); fig.savefig(FIGS / "01_top_bundles.png", dpi=160); plt.close(fig)

    r = rfm_sum.sort_values("share_of_revenue")
    fig, ax = plt.subplots(figsize=(8, 3.8))
    y = np.arange(len(r))
    ax.barh(y + 0.2, r["share_of_customers"] * 100, height=0.38, color=GREY, label="% of customers")
    ax.barh(y - 0.2, r["share_of_revenue"] * 100, height=0.38, color=BLUE, label="% of revenue")
    ax.set_yticks(y); ax.set_yticklabels(r["segment"])
    style(ax, "RFM segments: share of customers vs share of revenue", "%")
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout(); fig.savefig(FIGS / "02_rfm_segments.png", dpi=160); plt.close(fig)


# ------------------------------------------------------------------ Excel
HEAD, FILL, INPUT = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="2A78D6"), PatternFill("solid", fgColor="FFF4CC")


def add_table(wb, name, tname, df, fmts=None):
    ws = wb.create_sheet(name)
    ws.append(list(df.columns))
    for row in df.itertuples(index=False):
        ws.append([None if isinstance(v, float) and np.isnan(v) else v for v in row])
    t = Table(displayName=tname, ref=f"A1:{get_column_letter(df.shape[1])}{len(df) + 1}")
    t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    ws.add_table(t)
    for j, c in enumerate(df.columns, 1):
        ws.column_dimensions[get_column_letter(j)].width = max(10, min(38, len(c) + 3))
        if fmts and c in fmts:
            for r in range(2, len(df) + 2):
                ws.cell(r, j).number_format = fmts[c]
    ws.freeze_panes = "A2"
    return ws


def excel(rules: pd.DataFrame, strong: pd.DataFrame, rfm_sum: pd.DataFrame, v12: pd.DataFrame) -> None:
    XLSX.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ex = wb.active
    ex.title = "Bundle Explorer"

    # companions ranked within each anchor, keyed "anchor|rank" for INDEX/MATCH lookup
    r = rules.copy()
    r["rank"] = r.groupby("anchor")["attach_rate"].rank(method="first", ascending=False).astype(int)
    r = r[r["rank"] <= 10]
    anchors = (r.drop_duplicates("anchor")[["anchor", "anchor_desc", "anchor_orders"]]
                 .sort_values("anchor_orders", ascending=False).head(150))
    anchors["anchor_label"] = anchors["anchor_desc"].map(nice) + " [" + anchors["anchor"] + "]"
    r = r[r["anchor"].isin(anchors["anchor"])].merge(anchors[["anchor", "anchor_label"]], on="anchor")
    r["key"] = r["anchor_label"] + "|" + r["rank"].astype(str)
    lookup = r[["key", "anchor_label", "rank", "companion", "companion_desc", "companion_category", "attach_rate",
                "lift", "pair_orders", "orders_anchor_without_companion", "companion_avg_price"]]
    add_table(wb, "Data_Companions", "tblCompanions", lookup,
              {"attach_rate": "0.0%", "lift": "0.0", "companion_avg_price": "£#,##0.00"})
    add_table(wb, "Data_Anchors", "tblAnchors", anchors[["anchor_label", "anchor", "anchor_orders"]])
    add_table(wb, "Strong Rules", "tblStrongRules", strong.drop(columns=["n_orders"]),
              {"support": "0.00%", "attach_rate": "0.0%", "lift": "0.0", "lift_2010": "0.0", "lift_2011": "0.0",
               "companion_avg_price": "£#,##0.00"})
    add_table(wb, "RFM Segments", "tblRFM", rfm_sum,
              {"share_of_customers": "0.0%", "share_of_revenue": "0.0%", "revenue": "£#,##0"})
    add_table(wb, "12m Value by Cohort", "tblValue12m", v12,
              {"avg_value_12m": "£#,##0", "median_value_12m": "£#,##0", "repeat_rate_12m": "0.0%"})

    ex["B1"] = "Bundle Explorer"; ex["B1"].font = Font(bold=True, size=16)
    ex["B3"] = "Anchor product"
    ex["C3"] = anchors["anchor_label"].iloc[0]
    ex["C3"].fill = INPUT; ex["C3"].font = Font(bold=True)
    dv = DataValidation(type="list", formula1=f"=Data_Anchors!$A$2:$A${len(anchors) + 1}")
    ex.add_data_validation(dv); dv.add("C3")
    ex["B4"] = "Orders containing it"
    ex["C4"] = "=INDEX(tblAnchors[anchor_orders],MATCH($C$3,tblAnchors[anchor_label],0))"
    ex["C4"].number_format = "#,##0"
    ex["B5"] = "Pick any of the 150 highest-volume products in the yellow cell."
    ex["B5"].font = Font(italic=True, color="52514E")

    hdr = ["Rank", "Companion product", "Category", "Attach rate", "Lift", "Orders with both",
           "Anchor orders WITHOUT it", "Companion avg price", "Revenue if 10% of gap attached"]
    for j, h in enumerate(hdr, start=2):
        c = ex.cell(7, j, h); c.font, c.fill = HEAD, FILL
    for i in range(1, 11):
        row = 7 + i
        key = f'$C$3&"|"&{i}'
        ex.cell(row, 2, i)

        def lk(col, row=row, key=key):
            return f'=IFERROR(INDEX(tblCompanions[{col}],MATCH({key},tblCompanions[key],0)),"")'
        ex.cell(row, 3, lk("companion_desc"))
        ex.cell(row, 4, lk("companion_category"))
        ex.cell(row, 5, lk("attach_rate")).number_format = "0.0%"
        ex.cell(row, 6, lk("lift")).number_format = "0.0"
        ex.cell(row, 7, lk("pair_orders")).number_format = "#,##0"
        ex.cell(row, 8, lk("orders_anchor_without_companion")).number_format = "#,##0"
        ex.cell(row, 9, lk("companion_avg_price")).number_format = "£#,##0.00"
        ex.cell(row, 10, f'=IF(H{row}="","",H{row}*$C$20*I{row})').number_format = "£#,##0"
    ex["B20"] = "Scenario: share of the gap converted (input)"
    ex["C20"] = 0.10; ex["C20"].number_format = "0%"; ex["C20"].fill = INPUT
    ex["B21"] = ("The last column is a what-if, not a forecast: orders that had the anchor but not the companion "
                 "x the share you assume a prompt could convert x one unit at the companion's average price.")
    ex["B21"].font = Font(italic=True, color="52514E")
    ex.column_dimensions["A"].width = 2
    ex.column_dimensions["B"].width = 44
    ex.column_dimensions["C"].width = 40
    for c in "DEFGHIJ":
        ex.column_dimensions[c].width = 16
    ex.sheet_view.showGridLines = False
    wb.save(XLSX)


# ------------------------------------------------------------------ main
def main() -> None:
    TABLES.mkdir(parents=True, exist_ok=True)
    q = sql_blocks()
    with sqlite3.connect(DB) as con:
        pairs = pd.read_sql(q["frequent_pairs"], con,
                            params={"min_item_orders": MIN_ITEM_ORDERS, "min_pair_orders": MIN_PAIR_ORDERS})
        cat = pd.read_sql(q["category_attach"], con)
        size = pd.read_sql(q["basket_size_profile"], con)
        lines = pd.read_sql("SELECT invoice, stock_code, invoice_date FROM basket_line", con)
        orders = pd.read_sql("SELECT * FROM orders", con)
        products = pd.read_sql("SELECT * FROM product", con)

    # independent cross-check of the SQL pair counts
    multi = set(orders.loc[orders["distinct_products"] >= 2, "invoice"])
    py = pair_counts_sparse(lines[lines["invoice"].isin(multi)], MIN_ITEM_ORDERS, MIN_PAIR_ORDERS)
    chk = pairs.merge(py, on=["item_a", "item_b"], how="outer", suffixes=("_sql", "_py"), indicator=True)
    assert (chk["_merge"] == "both").all() and (chk["pair_orders_sql"] == chk["pair_orders_py"]).all(), \
        "SQL and Python pair counts disagree"

    # what happens with no minimum support (for the write-up)
    ml = lines[lines["invoice"].isin(multi)]
    item_n = ml.groupby("stock_code")["invoice"].nunique()
    singles = ml[ml["stock_code"].map(item_n) == 1].groupby("invoice").size()
    noise_rules = int((singles * (singles - 1)).sum())          # directional rules with conf 100%, lift = N

    rules = rules_from_pairs(pairs, products)
    rules = pd.concat([rules, yearly_lift(lines, orders, rules)], axis=1)
    rules["stable_both_years"] = ((rules["lift_2010"] >= 2) & (rules["lift_2011"] >= 2)).astype(int)
    strong = (rules[(rules["attach_rate"] >= STRONG_CONF) & (rules["lift"] >= STRONG_LIFT)]
                .sort_values("attach_rate", ascending=False).reset_index(drop=True))
    # one row per pair for the headline chart (keep the stronger direction)
    strong["pair_key"] = strong[["anchor", "companion"]].apply(lambda s: "|".join(sorted(s)), axis=1)
    top = strong.drop_duplicates("pair_key").head(15)
    strong = strong.drop(columns="pair_key")

    cust, rfm_sum = rfm(orders)
    v12 = value_12m(orders)

    for name, df in {"association_rules_all": rules, "strong_rules": strong, "category_attach": cat,
                     "basket_size_profile": size, "rfm_segments": rfm_sum, "value_12m_by_cohort": v12}.items():
        df.to_csv(TABLES / f"{name}.csv", index=False)
    cust.to_csv(ROOT / "data" / "processed" / "customer_rfm.csv", index=False)

    charts(top, rfm_sum)
    excel(rules, strong, rfm_sum, v12)
    (ROOT / "reports" / "findings.md").write_text(
        findings(pairs, rules, strong, top, size, rfm_sum, v12, noise_rules, len(multi)))
    print(f"pairs={len(pairs)} rules={len(rules)} strong={len(strong)} stable={strong.stable_both_years.mean():.0%}")


def findings(pairs, rules, strong, top, size, rfm_sum, v12, noise_rules, n_multi) -> str:
    cross = strong[(strong["anchor_category"] != strong["companion_category"])
                   & (strong["anchor_category"] != "Other") & (strong["companion_category"] != "Other")]
    cross.to_csv(TABLES / "cross_category_rules.csv", index=False)
    big = size[size["products_in_order"].isin(["11-25", "26-50", "51+"])]
    t1 = top.iloc[0]
    gap = strong.sort_values("orders_anchor_without_companion", ascending=False).iloc[0]
    rs = rfm_sum.set_index("segment")
    champs = rs.loc["Champions"]
    lapsed_loyal = rs.loc["Loyal - slipping"] if "Loyal - slipping" in rs.index else None
    v = v12.set_index("cohort_quarter")
    new = v12[~v12["cohort_quarter"].str.contains("existing")]
    same_cat = (strong["anchor_category"] == strong["companion_category"]).mean()
    lines = [
        "# Findings", "",
        f"_Generated by `src/analysis.py`. {n_multi:,} multi-product orders (UCI Online Retail II, Dec 2009 - Dec 2011). "
        f"Minimum support: product in {MIN_ITEM_ORDERS}+ orders, pair in {MIN_PAIR_ORDERS}+ orders._", "",
        "## 1. Minimum support is not optional",
        f"- With no threshold, products sold only once produce **{noise_rules:,} rules** with 100% confidence and a lift of "
        f"{n_multi:,}. They are artefacts of a single order.",
        f"- With the thresholds: {len(pairs):,} product pairs -> {len(rules):,} directional rules; "
        f"**{len(strong):,}** meet attach rate >= {STRONG_CONF:.0%} and lift >= {STRONG_LIFT:.0f}.",
        f"- Pair counts from the SQL Apriori query and an independent Python sparse-matrix count match exactly.", "",
        "## 2. Bundles that hold up",
        f"- Strongest: **{nice(t1.anchor_desc)} -> {nice(t1.companion_desc)}**: {t1.attach_rate:.0%} of orders with the "
        f"first also contain the second (lift {t1.lift:.0f}x, {int(t1.pair_orders):,} orders).",
        f"- {strong['stable_both_years'].mean():.0%} of strong rules keep a lift of 2x+ in **both** 2010 and 2011 baskets "
        "when recomputed year by year, so they are not one-season effects.",
        f"- {same_cat:.0%} of strong rules pair products in the same category - mostly colour/design variants of one item "
        "(e.g. the same bag in different prints). These are 'complete the set' prompts rather than cross-category bundles.",
        "- Even the top rules that cross keyword categories are mostly sibling designs (" + "; ".join(
            f"{nice(r.anchor_desc)} -> {nice(r.companion_desc)}" for r in cross.head(2).itertuples()) +
        "). Real cross-sell candidates sit further down `reports/tables/cross_category_rules.csv`; grouping products "
        "into families before mining would be the next step.",
        f"- Largest attach gap among strong rules: {int(gap.orders_anchor_without_companion):,} orders contained "
        f"{nice(gap.anchor_desc)} without {nice(gap.companion_desc)} (attach rate {gap.attach_rate:.0%}). "
        "The Excel Bundle Explorer turns any such gap into a what-if revenue figure from an assumed conversion share.", "",
        "## 3. Who the baskets belong to",
        f"- Orders with 11+ different products are {big['orders'].sum() / size['orders'].sum():.0%} of orders but "
        f"{big['share_of_revenue'].sum():.0%} of merchandise revenue - consistent with the source describing many "
        "customers as wholesalers. Bundle prompts matter most in this assortment-building context.",
        f"- Category-level attach is high everywhere (large mixed baskets), so product-level rules are the useful level.", "",
        "## 4. Customer value",
        f"- **Champions** (recent and frequent) are {champs.share_of_customers:.0%} of identified customers and "
        f"{champs.share_of_revenue:.0%} of their revenue.",
    ]
    if lapsed_loyal is not None:
        lines.append(f"- **Loyal - slipping** (frequent buyers, not seen recently): {int(lapsed_loyal.customers):,} customers, "
                     f"{lapsed_loyal.share_of_revenue:.0%} of revenue - the win-back list.")
    lines += [
        f"- 12-month merchandise spend (before cancellations) of newly acquired customers ranges from GBP {new['avg_value_12m'].min():,.0f} to "
        f"GBP {new['avg_value_12m'].max():,.0f} (average per customer by acquisition quarter); "
        f"{(new['repeat_rate_12m'] * new['customers']).sum() / new['customers'].sum():.0%} place a second order within a year. "
        "The Dec-2009 group is the pre-existing base and is shown separately.",
        "", "## Caveats",
        "- Association is not causation: a high lift says two products are bought together, not that "
        "recommending one will sell the other. Test prompts with an A/B split before rolling out.",
        "- Product categories come from keyword rules on descriptions (`src/etl.py`); 'Other' is ~9% of revenue.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
