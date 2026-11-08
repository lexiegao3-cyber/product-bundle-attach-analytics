-- =====================================================================
-- Market basket analysis in SQL (SQLite). Run by src/analysis.py.
-- Parameters are substituted by the runner:
--   :min_item_orders   minimum orders containing a product to consider it
--   :min_pair_orders   minimum orders containing BOTH products of a pair
--
-- Definitions (N = number of multi-product orders):
--   support(A,B)       = orders with A and B / N
--   confidence(A->B)   = orders with A and B / orders with A      ("attach rate" of B to A)
--   lift(A->B)         = confidence(A->B) / (orders with B / N)   (>1 = bought together more than chance)
--
-- Why the minimum-count thresholds matter: without them a pair that occurs
-- once, in two products that each sold once, gets confidence 100% and a lift
-- equal to N (tens of thousands). Those "rules" are noise, not bundles.
-- =====================================================================

-- name: frequent_pairs
WITH multi AS (                       -- baskets that can contain a pair at all
    SELECT invoice FROM orders WHERE distinct_products >= 2
),
n AS (SELECT COUNT(*) AS n_orders FROM multi),
item AS (                             -- Apriori step 1: frequent single products only
    SELECT b.stock_code, COUNT(*) AS item_orders
    FROM basket_line b JOIN multi m ON m.invoice = b.invoice
    GROUP BY b.stock_code
    HAVING COUNT(*) >= :min_item_orders
),
bl AS (                               -- basket lines restricted to frequent products
    SELECT b.invoice, b.stock_code
    FROM basket_line b
    JOIN multi m ON m.invoice = b.invoice
    JOIN item  i ON i.stock_code = b.stock_code
),
pair AS (                             -- Apriori step 2: pairs from frequent items only
    SELECT x.stock_code AS item_a, y.stock_code AS item_b, COUNT(*) AS pair_orders
    FROM bl x JOIN bl y ON x.invoice = y.invoice AND x.stock_code < y.stock_code
    GROUP BY x.stock_code, y.stock_code
    HAVING COUNT(*) >= :min_pair_orders
)
SELECT p.item_a, p.item_b, p.pair_orders,
       ia.item_orders AS item_a_orders, ib.item_orders AS item_b_orders, n.n_orders
FROM pair p
JOIN item ia ON ia.stock_code = p.item_a
JOIN item ib ON ib.stock_code = p.item_b
CROSS JOIN n;

-- name: category_attach
-- Of the orders that contain category A, what share also contain category B?
WITH multi AS (SELECT invoice FROM orders WHERE distinct_products >= 2),
oc AS (
    SELECT DISTINCT b.invoice, b.category
    FROM basket_line b JOIN multi m ON m.invoice = b.invoice
),
cat AS (SELECT category, COUNT(*) AS cat_orders FROM oc GROUP BY category),
n AS (SELECT COUNT(*) AS n_orders FROM multi)
SELECT x.category AS anchor_category, y.category AS attached_category,
       COUNT(*)                                          AS orders_with_both,
       c.cat_orders                                      AS anchor_orders,
       ROUND(1.0 * COUNT(*) / c.cat_orders, 4)           AS attach_rate,
       ROUND((1.0 * COUNT(*) / c.cat_orders) / (1.0 * cy.cat_orders / n.n_orders), 3) AS lift
FROM oc x
JOIN oc y   ON x.invoice = y.invoice AND x.category <> y.category
JOIN cat c  ON c.category = x.category
JOIN cat cy ON cy.category = y.category
CROSS JOIN n
GROUP BY x.category, y.category
ORDER BY x.category, attach_rate DESC;

-- name: basket_size_profile
SELECT CASE WHEN distinct_products = 1 THEN '1'
            WHEN distinct_products <= 5 THEN '2-5'
            WHEN distinct_products <= 10 THEN '6-10'
            WHEN distinct_products <= 25 THEN '11-25'
            WHEN distinct_products <= 50 THEN '26-50'
            ELSE '51+' END                    AS products_in_order,
       COUNT(*)                               AS orders,
       ROUND(AVG(order_value), 2)             AS avg_order_value,
       ROUND(SUM(order_value), 2)             AS revenue,
       ROUND(SUM(order_value) / (SELECT SUM(order_value) FROM orders), 4) AS share_of_revenue
FROM orders
GROUP BY 1
ORDER BY MIN(distinct_products);
