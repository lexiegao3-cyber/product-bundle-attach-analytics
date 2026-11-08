"""
ETL for basket analysis: UCI Online Retail II -> order baskets, product & customer tables.

Source: Chen, D. (2012). Online Retail II [Dataset]. UCI Machine Learning Repository.
https://doi.org/10.24432/C5CG6D (CC BY 4.0). Download instructions: data/README.md

What counts as a basket:
  * one invoice = one basket
  * only merchandise lines (5-digit stock codes and gift sets); postage, fees,
    manual lines, cancellations, write-offs and bad-debt adjustments are excluded
  * guest orders (no Customer ID) ARE baskets - what is bought together does not
    depend on knowing who bought it. They are left out of customer metrics only.
  * the 2010-12-01..09 invoices that appear in both source sheets are kept once
  * a product appearing on several lines of one invoice counts once for the basket
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "online_retail_II.xlsx"
CACHE = ROOT / "data" / "interim" / "online_retail_II_raw.pkl"
PROCESSED = ROOT / "data" / "processed"
WAREHOUSE = ROOT / "warehouse" / "basket.sqlite"
REPORTS = ROOT / "reports"
EXPECTED_ROWS = 1_067_371
PRODUCT_CODE = r"^(\d{5}[A-Za-z]{0,2}|DCGS\w+)$"

# keyword -> category, first match wins (order matters: "CHRISTMAS CANDLE" is Seasonal)
CATEGORY_RULES = [
    ("Seasonal",               r"CHRISTMAS|XMAS|EASTER|ADVENT|SNOWFLAKE|REINDEER|SANTA|HALLOWEEN"),
    ("Party & Celebration",    r"PARTY|BALLOON|BUNTING|BIRTHDAY|CONFETTI|WEDDING"),
    ("Lighting & Candles",     r"CANDLE|CANDLES|CHOCOLATECANDLE|LIGHT|LIGHTS|LANTERN|TEALIGHT|INCENSE|LAMP"),
    ("Jewellery & Accessories", r"NECKLACE|BRACELET|EARRINGS|RING|CHARM|DIAMANTE|BEAD|BROOCH|HAIR|PURSE|UMBRELLA|PARASOL|FAN|WARMER"),
    ("Kitchen & Dining",       r"MUG|MUGS|CUP|CUPS|PLATE|PLATES|BOWL|CAKE|CAKESTAND|TEA|TEAPOT|CUTLERY|JAR|BAKING|"
                               r"LUNCH|BOTTLE|SPOON|TRAY|NAPKINS?|KITCHEN|DINNER|JUG|EGG|APRON|OVEN|COASTER|CAKES|POPCORN|BOWLS|"
                               r"TEACUP|SAUCER|JAM|JARS|BREAD|PANTRY|MOULDS?|BEAKERS?|GLASSES|CHOPSTICKS|DOILY|DOILIES"),
    ("Bags & Storage",         r"BAG|BAGS|BOX|BOXES|TIN|TINS|STORAGE|BASKET|HAMPER|CASES?|DRAWER|SHOPPER|CHEST|CABINET|PEGS"),
    ("Stationery & Gift Wrap", r"CARD|CARDS|WRAP|PAPER|NOTEBOOK|PEN|PENCILS?|STICKERS?|TAPE|ENVELOPES?|TAGS?|CHALK|"
                               r"RIBBONS?|ERASER|DIARY|CALENDAR"),
    ("Garden & Outdoor",       r"GARDEN|GARDENERS|PLANT|PLANTER|WATERING|HERB|BIRDHOUSE|FEEDER|POT|SEEDS?"),
    ("Toys, Kids & Craft",    r"PAINT|COLOURING|CLAY|FELTCRAFT|GLIDERS?|SEWING|KNITTING|CRAFT|TOY|TOYS|DOLL|DOLLY|GAME|PLAYING|CHILDRENS|CHILDS|KIDS|TEDDY|JIGSAW|SPACEBOY|SOLDIER|"
                               r"SKIPPING|PUZZLE|RABBIT|BEAR"),
    ("Home Decor",             r"FRAME|MIRROR|CLOCK|SIGN|CUSHION|HOOK|HOOKS|DOORMAT|HEART|HEARTS|DECORATION|GARLAND|"
                               r"WALL|ORNAMENT|VASE|PLAQUE|HANGING|DOORSTOP|THROW|BLANKET|TILE|COAT|MEMOBOARD|MEMO|BOARD|"
                               r"CHALKBOARD|BLOCK|PEARS|ANTIQUE|SCULPTURE|FIGURE|WREATH"),
]


def load_raw() -> pd.DataFrame:
    if CACHE.exists():
        return pd.read_pickle(CACHE)
    if not RAW.exists():
        raise SystemExit(f"Missing {RAW} - see data/README.md")
    sheets = pd.read_excel(RAW, sheet_name=None, dtype={"Invoice": str, "StockCode": str})
    df = pd.concat([d.assign(source_sheet=n) for n, d in sheets.items()], ignore_index=True)
    assert len(df) == EXPECTED_ROWS, f"expected {EXPECTED_ROWS} rows, got {len(df)}"
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(CACHE)
    return df


def categorise(desc: pd.Series) -> pd.Series:
    d = desc.fillna("").str.upper()
    out = pd.Series("Other", index=desc.index, dtype=object)
    done = pd.Series(False, index=desc.index)
    for cat, pattern in CATEGORY_RULES:
        hit = d.str.contains(rf"\b(?:{pattern})\b", regex=True) & ~done
        out[hit] = cat
        done |= hit
    return out


def main() -> None:
    for p in (PROCESSED, WAREHOUSE.parent, REPORTS):
        p.mkdir(parents=True, exist_ok=True)
    log = []
    df = load_raw().rename(columns={"Invoice": "invoice", "StockCode": "stock_code", "Description": "description",
                                    "Quantity": "quantity", "InvoiceDate": "invoice_ts", "Price": "unit_price",
                                    "Customer ID": "customer_id", "Country": "country"})
    df["invoice"] = df["invoice"].astype(str).str.strip()
    df["stock_code"] = df["stock_code"].astype(str).str.strip()
    log.append(("raw lines", len(df)))

    s1, s2 = sorted(df["source_sheet"].unique())
    overlap = set(df.loc[df.source_sheet == s1, "invoice"]) & set(df.loc[df.source_sheet == s2, "invoice"])
    df = df[~((df.source_sheet == s2) & df.invoice.isin(overlap))]
    log.append((f"after removing {len(overlap):,} invoices duplicated across sheets", len(df)))

    keep = (~df.invoice.str[0].isin(["C", "A"]) & df.stock_code.str.match(PRODUCT_CODE)
            & (df.quantity > 0) & (df.unit_price > 0))
    df = df[keep].copy()
    log.append(("merchandise sale lines (no cancellations, fees, write-offs)", len(df)))

    df["line_revenue"] = (df["quantity"] * df["unit_price"]).round(2)
    df["invoice_date"] = df["invoice_ts"].dt.normalize()
    df["customer_id"] = df["customer_id"].astype("Int64")

    # one row per invoice x product (a product on several lines counts once in the basket)
    lines = (df.groupby(["invoice", "stock_code"], as_index=False)
               .agg(quantity=("quantity", "sum"), line_revenue=("line_revenue", "sum"),
                    invoice_date=("invoice_date", "min"), customer_id=("customer_id", "first"),
                    country=("country", "first")))
    log.append(("basket lines (invoice x product)", len(lines)))

    orders = (lines.groupby("invoice", as_index=False)
                   .agg(invoice_date=("invoice_date", "min"), customer_id=("customer_id", "first"),
                        country=("country", "first"), distinct_products=("stock_code", "size"),
                        units=("quantity", "sum"), order_value=("line_revenue", "sum")))
    orders["is_guest"] = orders["customer_id"].isna().astype(int)
    log.append(("orders (baskets)", len(orders)))
    log.append(("single-product orders (cannot contain a pair)", int((orders.distinct_products == 1).sum())))

    desc = (df.dropna(subset=["description"]).groupby(["stock_code", "description"]).size()
              .reset_index(name="n").sort_values(["stock_code", "n"], ascending=[True, False])
              .drop_duplicates("stock_code"))
    products = (lines.groupby("stock_code", as_index=False)
                     .agg(orders=("invoice", "nunique"), units=("quantity", "sum"), revenue=("line_revenue", "sum"))
                     .merge(desc[["stock_code", "description"]], on="stock_code", how="left"))
    products["description"] = products["description"].fillna("(no description)").str.strip()
    products["category"] = categorise(products["description"])
    products["avg_unit_price"] = (products["revenue"] / products["units"]).round(2)

    lines = lines.merge(products[["stock_code", "category"]], on="stock_code")

    WAREHOUSE.unlink(missing_ok=True)
    with sqlite3.connect(WAREHOUSE) as con:
        for name, t in {"basket_line": lines, "orders": orders, "product": products}.items():
            out = t.copy()
            for c in out.select_dtypes("datetime").columns:
                out[c] = out[c].dt.strftime("%Y-%m-%d")
            out.to_sql(name, con, index=False)
            out.to_csv(PROCESSED / f"{name}.csv", index=False)
        con.executescript("""
            CREATE INDEX ix_bl_invoice ON basket_line(invoice);
            CREATE INDEX ix_bl_stock   ON basket_line(stock_code);
            CREATE INDEX ix_o_cust     ON orders(customer_id);
        """)
    pd.DataFrame(log, columns=["step", "rows"]).to_csv(REPORTS / "data_prep_log.csv", index=False)
    for s, n in log:
        print(f"{n:>10,}  {s}")
    print(products["category"].value_counts().to_string())


if __name__ == "__main__":
    main()
