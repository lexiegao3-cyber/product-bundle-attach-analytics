# Power BI build guide

Run `python run_all.py` first. Then in Power BI Desktop (about 3 hours):

## 1. Load & model
1. Create the `ProjectFolder` text parameter; paste each block of `power_query/queries.pq` into a Blank Query.
2. Expected rows: `basket_line` 992,037 · `orders` 39,516 · `product` 4,907 · `customer_rfm` 5,852.
3. Relationships (single direction): `product → basket_line` on stock_code, `orders → basket_line` on invoice,
   `customer_rfm → orders` on customer_id. **Leave `Anchor` unconnected.**
4. Add a numeric-range what-if parameter `Gap Conversion` (0–0.5, step 0.01, default 0.10).
5. Add every measure from `measures.dax` to a `_Measures` table.

**Check:** pick *White Hanging Heart T-Light Holder* in an `Anchor[description]` slicer. In a table of
`product[description]`, *Red Hanging Heart T-Light Holder* should show Orders With Both **1,234**,
Anchor Orders **5,311** and Attach Rate **23.2%**. These are the numbers in `excel/bundle_explorer.xlsx`.

## 2. Pages
| Page | Content |
|---|---|
| **Bundle overview** | Cards: Orders, Avg Products per Order, Multi-product Orders · table from `strong_rules` (anchor, companion, attach rate, lift, lift 2010/2011) with a slicer on `stable_both_years` · bar: orders by basket-size band |
| **Attach explorer** | Slicer: `Anchor[description]` (single select, search on) · table: `product[description]`, Orders With Both, Attach Rate, Lift, Anchor Orders Without Product, Attach Gap Revenue (what-if) · visual-level filter Companion Rank ≤ 10 · Gap Conversion slicer |
| **Category view** | Matrix: anchor category × companion category from `reports/tables/category_attach.csv` (add as another query) with lift as diverging background colour |
| **Customers** | Bar: customers and revenue by `customer_rfm[segment]` · scatter: frequency vs monetary coloured by segment · table of "Loyal - slipping" customers for a win-back list |

## 3. Publish
Same as any report: publish to a workspace, Viewer access for merchandising/marketing,
scheduled refresh through OneDrive/SharePoint or a gateway, and an App for distribution.
