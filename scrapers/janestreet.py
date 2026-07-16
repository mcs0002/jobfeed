"""Jane Street's public first-party jobs JSON feed."""
from ._http import make_session

URL = "https://www.janestreet.com/jobs/main.json"
BASE = "https://www.janestreet.com/join-jane-street/position/{job_id}/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}

CITY_NAMES = {
    "AMS": "Amsterdam",
    "ATX": "Austin",
    "CHI": "Chicago",
    "HKG": "Hong Kong",
    "LDN": "London",
    "MUM": "Mumbai",
    "NYC": "New York",
    "NYC/HKG": "New York / Hong Kong",
    "SF": "San Francisco",
    "SGP": "Singapore",
    "SHA": "Shanghai",
}


def scrape() -> list[dict]:
    response = make_session().get(URL, headers=HEADERS, timeout=20)
    response.raise_for_status()

    jobs = []
    for job in response.json():
        job_id = job.get("id")
        title = job.get("position", "").strip()
        if not job_id or not title:
            continue
        # The feed carries the early-career signal in `availability`
        # ("Full-Time: New Grad", "Summer Internship", "Winter Internship"),
        # not the title — JS titles are plain ("Quantitative Trader"). Fold it
        # into the title so the internship-detector and the Haiku seniority
        # tagger can see it; otherwise all 30+ new-grad/intern roles look lateral.
        avail = job.get("availability", "").strip()
        if avail and avail != "Full-Time: Experienced":
            title = f"{title} ({avail.replace('Full-Time: ', '')})"
        city = job.get("city", "")
        jobs.append({
            "id": f"js_{job_id}",
            "title": title,
            "url": BASE.format(job_id=job_id),
            "location": CITY_NAMES.get(city, city),
            "posted": "",
        })
    return jobs
