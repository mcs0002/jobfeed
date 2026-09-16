"""Bank for International Settlements vacancy scraper.

BIS rebuilt its website (observed 2026-09-02) and the JSON API this scraper
relied on is gone: ``/api/document_lists/vacancies.json`` now returns 404,
which is why the source was *erroring* rather than quietly returning 0. There
is no replacement JSON endpoint — ``/api/vacancies.json`` 404s as well, and the
new site ships no ``__NEXT_DATA__``, no ``__NUXT__`` and no JSON-LD — so this
parses the server-rendered HTML instead. Two hops:

  1. ``/about/careers/vacancies`` — cards linking to ``/vacancy/jrNNNNNN``,
     each carrying the title and a "Geographical location" highlight.
  2. ``/vacancy/jrNNNNNN`` — the posting text and the Workday apply URL.

BIS still runs hiring on Workday (``bis.wd3.myworkdayjobs.com``). The apply
link from the detail page is preferred as the job URL so an applicant lands on
the form rather than on the description again.

The requisition id is upper-cased on purpose. The old code took it from the
API's ``job_requisition_id`` field, which was upper case, so existing rows are
stored as ``bis_JR100469``. The new site only exposes it lower case in the URL
path, and taking it verbatim would mint ``bis_jr100469`` — a different id, which
would delist every live BIS row and re-insert it as new in the same run.

Known regression: the new site publishes no posting date, so ``posted`` is
empty. It does show an "Apply by" date, but ``deadline`` is owned by the tagger
(derived from the description), not by scrapers, so it is deliberately not set
here rather than introducing a second writer for one column.
"""
from bs4 import BeautifulSoup

from ._http import make_session

BASE = "https://www.bis.org"
LIST_URL = f"{BASE}/about/careers/vacancies"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
}


def _highlight(card, label: str) -> str:
    """Read one labelled value out of a vacancy card's highlight list."""
    for item in card.select("li.card-highlighted__item"):
        lab = item.select_one(".card-highlighted__label")
        val = item.select_one(".card-highlighted__value")
        if lab and val and label.lower() in lab.get_text(strip=True).lower():
            return val.get_text(strip=True)
    return ""


def scrape() -> list[dict]:
    session = make_session()
    resp = session.get(LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    links = [
        a for a in soup.find_all("a", href=True)
        if a["href"].startswith("/vacancy/")
    ]

    jobs: list[dict] = []
    seen: set[str] = set()

    for link in links:
        path = link["href"]
        jrid = path.rsplit("/", 1)[-1].strip().upper()
        # The same vacancy can appear in more than one card slot on the page.
        if not jrid or jrid in seen:
            continue
        seen.add(jrid)

        heading = link.select_one(".card-heading")
        title = heading.get_text(strip=True) if heading else ""
        location = _highlight(link, "Geographical location")

        url = f"{BASE}{path}"
        description = ""

        detail = session.get(url, headers=HEADERS, timeout=30)
        if detail.ok:
            dsoup = BeautifulSoup(detail.text, "html.parser")

            if not title:
                h1 = dsoup.find("h1")
                if h1:
                    # Detail headings read "Title - Location"; keep the title.
                    title = h1.get_text(strip=True).split(" - ")[0].strip()

            apply_link = dsoup.find(
                "a", href=lambda h: h and "myworkdayjobs.com" in h
            )
            if apply_link:
                url = apply_link["href"]

            # The posting body is `div.text__component`. Do NOT fall back to
            # "biggest element matching [class*=content]": on this site that
            # resolves to a wrapper containing the whole global nav, which
            # yields a byte-identical 7.2 KB of BIS boilerplate for every
            # vacancy — plausible-looking text that would silently poison the
            # tagger. Empty is better than uniform.
            blocks = dsoup.select("div.text__component")
            if blocks:
                description = "\n\n".join(
                    t for t in (b.get_text(" ", strip=True) for b in blocks) if t
                )

        if not title:
            continue

        jobs.append({
            "id": f"bis_{jrid}",
            "title": title,
            "url": url,
            "location": location,
            "description": description,
            "posted": "",
        })

    return jobs
