"""Browse 'All' leaves wealth out; the Wealth tab still lists it (2026-10-10)."""
from starlette.requests import Request

import web.app as A


def _req(query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                    "query_string": query.encode(), "cookies": {}})


def test_all_view_hides_wealth_but_wealth_tab_shows_it(monkeypatch):
    monkeypatch.setattr(A, "_hidden_companies", lambda request: [])
    monkeypatch.setattr(A, "_is_guest", lambda request: False)
    assert A._filters_from_request(_req(""))["hide_wealth"] is True
    assert A._filters_from_request(_req("area=wealth"))["hide_wealth"] is False
    assert A._filters_from_request(_req("area=markets"))["hide_wealth"] is False


def test_page_limit_is_clamped_and_more_url_drops_old_pages(monkeypatch):
    monkeypatch.setattr(A, "_is_guest", lambda request: False)
    assert A._page_limit(_req(""))[1] == A.PAGE_LIMIT
    assert A._page_limit(_req("pages=3"))[1] == 3 * A.PAGE_LIMIT
    assert A._page_limit(_req("pages=999"))[0] == A.MAX_PAGES
    assert A._page_limit(_req("pages=x"))[0] == 1
    url = A._more_url(_req("area=quant&pages=2"), "/", 2)
    assert "pages=3" in url and "area=quant" in url and url.count("pages=") == 1
