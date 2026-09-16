#!/usr/bin/env python3
"""Find the current early-careers URL for firms the sweep could not read.

Companion to campus_sweep.py, run after a sweep leaves rows in the
"unreadable" bucket. Those rows are mostly one of three things: a URL that has
moved (the firm redesigned its careers site), a page whose content lives
somewhere else on the same domain, or a genuine wall. This tells them apart
without guessing, and without a browser.

For each failing firm it:
  1. probes a short ladder of conventional early-careers paths on the same
     domain (/careers, /early-careers, /graduates, ...);
  2. reads the domain's sitemap(s) and the homepage's own links, keeping URLs
     whose path or link text matches graduate/campus/student/trainee/intern;
  3. fetches each candidate through campus_sweep.fetch_page — the same ladder
     the sweep itself uses — and keeps the ones that return real text.

It prints candidates ranked by how much readable text they yield and never
edits anything: the URL that belongs in targets.json or GRAD_SCHEMES.md is a
judgement call, and a plausible-looking careers page for the wrong division is
worse than a known gap.

  python campus_probe.py                       # every failed firm in the season
  python campus_probe.py --only KKR --only Bain
  python campus_probe.py --season 2026-09 --json out.json
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import campus_sweep as cs  # noqa: E402

CANDIDATE_PATHS = (
    "/careers", "/careers/", "/careers/early-careers", "/careers/students",
    "/careers/graduates", "/early-careers", "/early-careers/",
    "/graduates", "/graduate-programme", "/graduate-programmes",
    "/students", "/students-and-graduates", "/campus", "/university",
    "/en/careers", "/en/careers/early-careers", "/careers/campus",
    "/careers/university-students", "/about/careers", "/join-us/graduate",
)
KEYWORD_RE = re.compile(
    r"graduate|early.?career|student|campus|trainee|internship|intern\b|"
    r"school.?leaver|apprentice|young.?talent|young.?professional",
    re.IGNORECASE,
)
# Paths that match the keywords but are never the programme page.
NOISE_RE = re.compile(r"/(news|insights?|blog|press|media|research|events?)/",
                      re.IGNORECASE)
SITEMAPS = ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt")
MAX_CANDIDATES = 8


def _root(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


PROBE_TIMEOUT = 10


def _try(url: str) -> tuple[int, str]:
    """Cheap look at one candidate: a single browser identity and a short
    timeout. Discovery walks tens of URLs per firm, so it cannot afford the
    sweep's full ladder — that runs once, on the survivors, in _verify."""
    try:
        r = cs._get(url, "chrome", timeout=PROBE_TIMEOUT)
    except Exception as exc:
        return 0, f"fetch_error: {type(exc).__name__}"
    if r.status_code >= 400:
        return 0, f"http_{r.status_code}"
    raw = r.text or ""
    text = cs._extract_text(raw, cs.MAX_PAGE_CHARS)
    if len(text) < cs.MIN_USEFUL_CHARS:
        text = cs._embedded_json_text(raw) or text
    return len(text), ""


def _verify(url: str) -> tuple[int, str]:
    """The real thing: the sweep's own ladder, so a candidate that survives is
    one the sweep will actually be able to read."""
    text, err = cs.fetch_page(url)
    return len(text), err


def _links_from_homepage(base: str) -> list[str]:
    text_html = ""
    for imp in cs._IMPERSONATIONS:
        try:
            r = cs._get(base, imp)
        except Exception:
            continue
        if r.status_code < 400:
            text_html = r.text or ""
            break
    if not text_html:
        return []
    out = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         text_html, re.DOTALL | re.IGNORECASE):
        href, label = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        if KEYWORD_RE.search(href) or KEYWORD_RE.search(label):
            full = urljoin(base, href)
            if full.startswith("http") and not NOISE_RE.search(full):
                out.append(full.split("#")[0])
    return out


def _links_from_sitemap(base: str) -> list[str]:
    out = []
    for path in SITEMAPS:
        try:
            r = cs._get(base + path, "chrome")
        except Exception:
            continue
        if r.status_code >= 400:
            continue
        body = r.text or ""
        urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        urls += re.findall(r"(?im)^sitemap:\s*(\S+)", body)
        for u in urls[:4000]:
            if u.endswith(".xml") and KEYWORD_RE.search(u) is None:
                continue
            if KEYWORD_RE.search(u) and not NOISE_RE.search(u):
                out.append(u)
        if out:
            break
    return out


def probe(name: str, url: str) -> dict:
    base = _root(url)
    # If the sweep's own ladder now reads the original URL, there is nothing to
    # discover — the URL was never the problem.
    chars, err = _verify(url)
    if not err and chars >= cs.MIN_USEFUL_CHARS:
        return {"name": name, "original_url": url, "still_ok": True,
                "candidates": [{"url": url, "chars": chars}]}
    seen, candidates = {url}, []
    # Path guessing is only valid on a firm's own domain. On a shared ATS host
    # (job-boards.greenhouse.io/<slug>) a guessed path is a DIFFERENT COMPANY's
    # board, which would read fine and be entirely wrong — the worst possible
    # outcome for this tool.
    pool = [] if cs._ATS_HOST_RE.search(base) else [base + p for p in CANDIDATE_PATHS]
    if not cs._ATS_HOST_RE.search(base):
        pool += _links_from_sitemap(base)
        pool += _links_from_homepage(base)
    for cand in pool:
        if cand in seen:
            continue
        seen.add(cand)
        chars, err = _try(cand)
        if not err and chars >= cs.MIN_USEFUL_CHARS:
            candidates.append({"url": cand, "chars": chars})
            if len(candidates) >= MAX_CANDIDATES:
                break
    # A site that serves the same page for every path (an SPA catch-all) makes
    # every guess look like a hit. Byte-identical lengths across different
    # paths is the tell; say so rather than proposing four URLs for one page.
    sizes = [c["chars"] for c in candidates]
    catch_all = len(sizes) > 2 and len(set(sizes)) == 1
    candidates.sort(key=lambda c: -c["chars"])
    verified = []
    for c in candidates[:4]:
        chars, err = _verify(c["url"])
        if not err and chars >= cs.MIN_USEFUL_CHARS:
            verified.append({"url": c["url"], "chars": chars})
    verified.sort(key=lambda c: -c["chars"])
    return {"name": name, "original_url": url, "candidates": verified,
            "catch_all_suspected": catch_all}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2026-09")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--json", help="write the findings to this path")
    args = ap.parse_args()

    results = cs.OUT_ROOT / args.season / "results.jsonl"
    rows = [json.loads(l) for l in results.read_text().splitlines() if l.strip()]
    bad = [r for r in rows if r.get("error")]
    if args.only:
        needles = [n.lower() for n in args.only]
        bad = [r for r in bad if any(n in r["name"].lower() for n in needles)]
    print(f"probing {len(bad)} firm(s)\n", flush=True)

    out = []
    for i, r in enumerate(bad, 1):
        found = probe(r["name"], r["url"])
        out.append(found)
        head = f"[{i}/{len(bad)}] {r['name']} ({str(r.get('error')).split(':')[0]})"
        if found.get("still_ok"):
            print(head + "  -> original URL now reads fine", flush=True)
        elif found["candidates"]:
            if found.get("catch_all_suspected"):
                head += "  [same page for every path — SPA catch-all, pick the hub]"
            print(head, flush=True)
            for c in found["candidates"][:4]:
                print(f"    {c['chars']:6d} chars  {c['url']}", flush=True)
        else:
            print(head + "  -> nothing found on the domain", flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
