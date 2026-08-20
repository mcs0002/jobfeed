"""Front-office-markets vocabulary for the few boards still positively scoped.

Most giant boards moved to negative-scope (heavy executor pulls the full board,
filter.py drops the ops/retail/IT noise, the Haiku tagger sorts divisions). The
exception is a board that exposes only a flat list with no division facets and
is mostly retail — Wells Fargo's bank-wide sitemap — where a positive slug
filter is still the cheapest cut to its Securities (markets) arm.

Kept as the single source so that filter (scrapers/wellsfargo.py) doesn't carry
its own divergent hand-list. tests/test_vocab.py asserts no term here collides
with a filter.py drop set, so the positive cut stays strictly more permissive
than the negative gates (the non-redundancy contract).
"""

# Substring terms matched against a job's URL slug (Wells Fargo). Markets-
# specific on purpose: bare "sales"/"advisor" would drag in retail.
FRONT_OFFICE_KEYWORDS = (
    # desks & functions
    "markets", "trading", "trader", "sales-and-trading",
    "structuring", "structurer", "syndicate",
    "quant", "quantitative", "strategist",
    # asset classes
    "fixed-income", "ficc", "rates", "credit-trading",
    "equities", "equity-research", "equity-derivatives",
    "derivative", "fx", "foreign-exchange", "commodities",
    # markets-adjacent front office
    "securities", "capital-markets",
    "prime-brokerage", "market-maker", "market-making",
    "electronic-trading", "exotics", "securitized", "securitised",
    "investment-bank", "investment-banking",
)
