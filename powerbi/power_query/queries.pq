// =====================================================================
// Power Query (M). Create a Text parameter ProjectFolder first
// (full path to this repo, ending in "\"), run `python run_all.py`,
// then paste each block into a Blank Query and name it as in the comment.
// =====================================================================


// ---------- query: basket_line
let
    Source  = Csv.Document(File.Contents(ProjectFolder & "data\processed\basket_line.csv"),
                           [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed   = Table.TransformColumnTypes(Headers, {
        {"invoice", type text}, {"stock_code", type text}, {"quantity", Int64.Type},
        {"line_revenue", Currency.Type}, {"invoice_date", type date}, {"customer_id", Int64.Type},
        {"country", type text}, {"category", type text}}, "en-US")
in
    Typed


// ---------- query: orders
let
    Source  = Csv.Document(File.Contents(ProjectFolder & "data\processed\orders.csv"),
                           [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed   = Table.TransformColumnTypes(Headers, {
        {"invoice", type text}, {"invoice_date", type date}, {"customer_id", Int64.Type},
        {"country", type text}, {"distinct_products", Int64.Type}, {"units", Int64.Type},
        {"order_value", Currency.Type}, {"is_guest", Int64.Type}}, "en-US")
in
    Typed


// ---------- query: product
let
    Source  = Csv.Document(File.Contents(ProjectFolder & "data\processed\product.csv"),
                           [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed   = Table.TransformColumnTypes(Headers, {
        {"stock_code", type text}, {"orders", Int64.Type}, {"units", Int64.Type},
        {"revenue", Currency.Type}, {"description", type text}, {"category", type text},
        {"avg_unit_price", type number}}, "en-US"),
    Proper  = Table.TransformColumns(Typed, {{"description", Text.Proper, type text}})
in
    Proper


// ---------- query: Anchor   (reference of product; keep NOT related to anything)
let
    Source = product,
    Keep   = Table.SelectColumns(Source, {"stock_code", "description", "category", "orders"}),
    // only products with enough orders to give stable attach rates
    Enough = Table.SelectRows(Keep, each [orders] >= 300)
in
    Enough


// ---------- query: customer_rfm
let
    Source  = Csv.Document(File.Contents(ProjectFolder & "data\processed\customer_rfm.csv"),
                           [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed   = Table.TransformColumnTypes(Headers, {
        {"customer_id", Int64.Type}, {"frequency", Int64.Type}, {"monetary", Currency.Type},
        {"recency_days", Int64.Type}, {"R", Int64.Type}, {"F", Int64.Type}, {"M", Int64.Type},
        {"segment", type text}}, "en-US"),
    Slim    = Table.RemoveColumns(Typed, {"last", "first"})
in
    Slim


// ---------- query: strong_rules   (pre-computed rules for the overview page)
let
    Source  = Csv.Document(File.Contents(ProjectFolder & "reports\tables\strong_rules.csv"),
                           [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed   = Table.TransformColumnTypes(Headers, {
        {"pair_orders", Int64.Type}, {"attach_rate", type number}, {"lift", type number},
        {"lift_2010", type number}, {"lift_2011", type number},
        {"orders_anchor_without_companion", Int64.Type}, {"stable_both_years", Int64.Type}}, "en-US")
in
    Typed
