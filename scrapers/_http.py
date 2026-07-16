"""Shared HTTP helpers for the scrapers.

The recurring bug this guards against: when a server returns ``text/html``
(or any ``text/*``) without a ``charset`` in the Content-Type header,
``requests`` follows RFC 2616 and falls back to ``ISO-8859-1`` for
``response.text``. Most European career sites are actually UTF-8, so that
fallback turns "Nestlé"/"Qualitäts"/"Crédit Agricole" into mojibake
("NestlÃ©" / "QualitÃ¤ts" / ...) the moment ``.text`` is read.

``fix_encoding`` corrects the response in place *before* ``.text`` /
``BeautifulSoup`` is read: if requests only has the ISO-8859-1 default (i.e.
the header carried no usable charset), it re-detects the real encoding from
the body via ``apparent_encoding`` (charset_normalizer). Responses that
declare their charset explicitly are left untouched, so this is a no-op for
the endpoints that were already correct.
"""
import subprocess

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


_MISSING = object()

_CURL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def curl_get(url: str, timeout: int = 40) -> str:
    """Fetch a URL's body via the system `curl` binary, returning the text.

    curl's TLS/JA3 fingerprint passes some Cloudflare bot-fight checks that plain
    `requests` trips (e.g. ADB), and macOS curl completes incomplete cert chains
    via AIA that `requests`/certifi reject (e.g. Mercuria). No extra Python
    dependency (curl ships with macOS). For JA3 impersonation inside Python,
    `curl_cffi` IS available on prod (verified 2026-06-29, v0.15.0) — used by
    e.g. eploy.py. Plain HTTP — policy-compliant, not a JS browser. Raises
    RuntimeError on a non-zero exit.

    --fail is load-bearing: without it an HTTP 4xx/5xx error page comes back
    as body text with exit 0, callers parse it to zero jobs, and the pipeline
    delists the whole source (curl exits 22 on HTTP errors instead)."""
    proc = subprocess.run(
        ["curl", "-sSL", "--fail", "--compressed", "--max-time", str(timeout),
         "-A", _CURL_UA, url],
        capture_output=True, text=True, timeout=timeout + 20,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"curl_get failed (exit {proc.returncode}) for {url}: "
            f"{proc.stderr.strip()[:200]}")
    return proc.stdout


def make_session() -> requests.Session:
    """Return a ``requests.Session`` that retries transient failures.

    Both ``http://`` and ``https://`` are mounted with an ``HTTPAdapter`` that
    retries on 429/500/502/503/504 with exponential backoff. The rationale: the
    ATS endpoints we scan sit behind Akamai/Cloudflare-style fronts that
    intermittently emit a 429 or 503 under load. On an unattended scheduled scan
    a single such blip would otherwise zero out an entire board (its parsed
    count then mismatches the reported total and the whole feed is discarded).
    Three retries with backoff turns those blips into a brief wait instead.

    Modelled on the inline session in ``scrapers/successfactors.py``; callers
    that need shared cookies/headers across pages should create one session per
    scrape call and reuse it across requests.
    """
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
    ))
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _needs_correction(response: requests.Response) -> bool:
    """True when requests has no real charset for the body.

    ``encoding`` is ``None`` when the header carried no usable charset (newer
    requests) or ``"ISO-8859-1"`` when it fell back to the legacy RFC-2616
    ``text/*`` default (older requests) or a server falsely declared Latin-1.
    All three are the mojibake condition for the UTF-8 European boards we
    scrape; any other declared charset (e.g. ``utf-8``) is trusted as-is.

    Objects without an ``encoding`` attribute (lightweight test doubles) never
    need correction — there is no resolved charset to override.
    """
    encoding = getattr(response, "encoding", _MISSING)
    if encoding is _MISSING:
        return False
    return encoding is None or encoding.lower() == "iso-8859-1"


def fix_encoding(response: requests.Response) -> requests.Response:
    """Re-detect ``response.encoding`` in place when requests has no real
    charset, then return the same response.

    Use right after ``raise_for_status()`` and before reading ``.text``::

        response = requests.get(url)
        response.raise_for_status()
        fix_encoding(response)
        soup = BeautifulSoup(response.text, "html.parser")
    """
    if _needs_correction(response):
        detected = response.apparent_encoding
        if detected:
            response.encoding = detected
    return response


def assert_complete(count: int, total: int | None, source: str, band: float = 0.9) -> None:
    """Raise if ``count`` is below ``band * total``, refusing a dangerously partial result.

    Doctrine: downstream delist/purge logic treats the scraper's return value as
    the *complete* board.  A silent partial (e.g. pagination broke after page 1)
    looks identical to a legitimately small board, so the missing tail is
    delisted on the next run.  This helper turns that silent data-loss into a
    loud failure.

    Parameters
    ----------
    count:
        Number of jobs actually fetched.
    total:
        Server-reported total.  When ``None`` or ``0`` the check is a no-op
        (the caller handles the no-total case separately).
    source:
        Short human-readable label for error messages
        (e.g. ``"Beesite/deutschebank"``).
    band:
        Fraction of ``total`` that must be present before we accept the result.
        Defaults to 0.9 (90 %) — the same threshold used across the fleet.
        Small over-counts (deduplication removing exact duplicates) and rounding
        at page boundaries mean perfect equality is not always achievable, hence
        the 10 % tolerance.
    """
    if not total:
        return
    if count < band * total:
        raise RuntimeError(
            f"{source}: fetched {count} of {total} reported jobs"
            " — refusing partial result"
        )


def fix_encoding_utf8(response: requests.Response) -> requests.Response:
    """Force UTF-8 when the source is known to be UTF-8 but ships no charset.

    Cheaper and more reliable than body sniffing when the endpoint is known.
    Only overrides the charset-less / Latin-1-fallback case, so an explicit
    server-declared charset still wins.
    """
    if _needs_correction(response):
        response.encoding = "utf-8"
    return response
