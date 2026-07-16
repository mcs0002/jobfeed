# scrapers package
#
# ATS dispatch registry. `HANDLERS` maps each `ats` string used in targets.json
# to a small named adapter `(company: dict) -> list[dict]` that prepares the
# arguments exactly as main.scrape_company used to inline them and calls the
# matching scraper module's `scrape`. This is a behavior-preserving refactor of
# the old if/elif ladder — each adapter reproduces that branch's logic verbatim.

import requests

from . import (
    abnamro,
    adp_careercenter,
    achmea,
    appellia,
    ashby,
    attrax,
    avature,
    balyasny,
    bamboohr,
    beesite,
    beesite_sitemap,
    bis,
    bpce,
    bnpparibas_paced,
    brassring,
    brassring_hosted,
    breezy,
    bundesbank,
    citadel,
    csod,
    deshaw,
    deutscheboerse,
    directemployers,
    eib,
    eightfold,
    erste_btp,
    euronext,
    generali,
    generic,
    getnoticed,
    glencore,
    goldman,
    greenhouse,
    guidecom,
    hr_manager,
    icims,
    koch_avature,
    kpmg_us,
    emply,
    hibob,
    intervieweb,
    janestreet,
    jibe,
    kernel,
    lever,
    oracle_hcm,
    ossiam,
    azimut,
    pfa,
    personio,
    phenom,
    phenom_widgets,
    pinpoint,
    peoplebank,
    rwe,
    radancy,
    recruitee,
    recsolu,
    refline,
    rss,
    sitemap_jobs,
    eploy,
    talentview,
    smartrecruiters,
    societegenerale,
    successfactors,
    successfactors_api,
    successfactors_classic,
    successfactors_dwr,
    talentsoft,
    talentbrew,
    talnet,
    teamio,
    teamtailor,
    umantis,
    uniper,
    ukg,
    vanlanschot,
    mckinsey,
    workable,
    wellsfargo,
    workday,
    wp_job,
    zoho_recruit,
)

# NOTE: workday_playwright is deliberately NOT imported here — it imports
# playwright.sync_api at module top, so a top-level import would make the
# ENTIRE scrapers package (and thus the nightly scan) fail to import the day
# playwright is uninstalled (the stated retirement direction). It is lazily
# imported inside the two adapters that can still route to it.


# --- Per-ATS adapters ------------------------------------------------------
# One named function per ats. Each takes the target dict and returns the scraped
# jobs list, doing the exact argument preparation the old ladder did.

def _greenhouse(company):
    return greenhouse.scrape(company["slug"], eu=company.get("eu", False),
                             url_template=company.get("url_template", ""))


def _bis(company):
    return bis.scrape()


def _brassring(company):
    return brassring.scrape(company["brassring"])


def _brassring_hosted(company):
    return brassring_hosted.scrape(company["brassring_hosted"])


def _glencore(company):
    return glencore.scrape(company.get("glencore", {}).get("keyword", ""))


def _uniper(company):
    return uniper.scrape()


def _lever(company):
    return lever.scrape(
        company["slug"],
        api_base=company.get("api_base", lever.DEFAULT_API_BASE),
    )


def _workday(company):
    # Behavior-preserving: try the JSON API first; on an HTTP 422 fall back to
    # the Playwright session-cookie path, carrying career_url onto the config.
    # Matched on the actual response status — the old '"422" in str(e)' also
    # fired on any exception whose message merely contained "422".
    try:
        return workday.scrape(company["workday"])
    except requests.HTTPError as e:
        if getattr(e.response, "status_code", None) == 422:
            from . import workday_playwright  # lazy — see note at the imports
            cfg = dict(company["workday"])
            if "career_url" in company:
                cfg["career_url"] = company["career_url"]
            return workday_playwright.scrape(cfg)
        raise


def _workday_playwright(company):
    from . import workday_playwright  # lazy — see note at the imports
    return workday_playwright.scrape(company["workday"])


def _ashby(company):
    return ashby.scrape(company["slug"])


def _smartrecruiters(company):
    return smartrecruiters.scrape(company["slug"], query=company.get("query"))


def _beesite(company):
    return beesite.scrape(company["beesite"])


def _beesite_sitemap(company):
    return beesite_sitemap.scrape(company["beesite_sitemap"])


def _bamboohr(company):
    return bamboohr.scrape({"slug": company["slug"]})


def _ossiam(company):
    return ossiam.scrape(company.get("language", "EN"))


def _generali(company):
    return generali.scrape(company.get("generali"))


def _vanlanschot(company):
    return vanlanschot.scrape(company.get("vanlanschot"))


def _refline(company):
    return refline.scrape(company["refline"])


def _hr_manager(company):
    return hr_manager.scrape(company["hr_manager"])


def _rss(company):
    return rss.scrape(company["rss"])


def _ukg(company):
    return ukg.scrape(company["ukg"])


def _appellia(company):
    return appellia.scrape(company["appellia"])


def _achmea(company):
    return achmea.scrape(company["achmea"])


def _getnoticed(company):
    return getnoticed.scrape(company["getnoticed"])


def _azimut(company):
    return azimut.scrape(company.get("azimut"))


def _pfa(company):
    return pfa.scrape(company.get("pfa"))


def _teamio(company):
    return teamio.scrape(company["teamio"])


def _zoho_recruit(company):
    return zoho_recruit.scrape(company["zoho_recruit"])


def _sitemap_jobs(company):
    return sitemap_jobs.scrape(company["sitemap_jobs"])


def _bpce(company):
    return bpce.scrape(company["bpce"])


def _bnpparibas_paced(company):
    return bnpparibas_paced.scrape()


def _balyasny(company):
    return balyasny.scrape()


def _goldman(company):
    return goldman.scrape(company.get("goldman"))


def _citadel(company):
    return citadel.scrape(company["citadel"])


def _csod(company):
    return csod.scrape(company["csod"])


def _eib(company):
    return eib.scrape(company.get("eib", {}))


def _breezy(company):
    return breezy.scrape(company["account"])


def _janestreet(company):
    return janestreet.scrape()


def _deshaw(company):
    return deshaw.scrape()


def _deutscheboerse(company):
    return deutscheboerse.scrape()


def _directemployers(company):
    return directemployers.scrape(company["directemployers"])


def _eightfold(company):
    return eightfold.scrape(company["eightfold"])


def _euronext(company):
    return euronext.scrape()


def _jibe(company):
    return jibe.scrape(company["jibe"])


def _emply(company):
    return emply.scrape(company["emply"])


def _hibob(company):
    return hibob.scrape(company["hibob"])


def _personio(company):
    return personio.scrape(company["personio"])


def _pinpoint(company):
    return pinpoint.scrape(company["feed_url"])


def _umantis(company):
    return umantis.scrape(company["umantis"])


def _rwe(company):
    return rwe.scrape(company["rwe"])


def _workable(company):
    return workable.scrape(company["account"],
                           widget_account=company.get("widget_account", ""))


def _mckinsey(company):
    return mckinsey.scrape()


def _wellsfargo(company):
    if "keywords" in company:
        return wellsfargo.scrape(company["keywords"])
    return wellsfargo.scrape()


def _successfactors(company):
    return successfactors.scrape(
        company["base_url"],
        search_params=company.get("search_params"),
    )


def _successfactors_api(company):
    return successfactors_api.scrape(company["successfactors_api"])


def _successfactors_classic(company):
    return successfactors_classic.scrape(company["successfactors_classic"])


def _successfactors_dwr(company):
    return successfactors_dwr.scrape(company["successfactors_dwr"])


def _erste_btp(company):
    return erste_btp.scrape(company["erste_btp"])


def _talentbrew(company):
    return talentbrew.scrape(company["talentbrew"])


def _talnet(company):
    return talnet.scrape(
        company["board_url"],
        fetch_detail=company.get("talnet_fetch_detail", True),
    )


def _teamtailor(company):
    return teamtailor.scrape(company["base_url"])


def _bundesbank(company):
    return bundesbank.scrape()


def _abnamro(company):
    return abnamro.scrape()


def _avature(company):
    return avature.scrape(
        company["search_url"],
        page_size=company.get("page_size", 9),
        follow_until_empty=company.get("follow_until_empty", False),
    )


def _attrax(company):
    return attrax.scrape(company["attrax"])


def _radancy(company):
    return radancy.scrape(company["search_url"])


def _recruitee(company):
    return recruitee.scrape(company["slug"])


def _recsolu(company):
    return recsolu.scrape(company["recsolu"])


def _kernel(company):
    return kernel.scrape(company["kernel"])


def _talentview(company):
    return talentview.scrape(company["talentview"])


def _eploy(company):
    return eploy.scrape(company["eploy"])


def _adp_careercenter(company):
    return adp_careercenter.scrape(company["adp_careercenter"])


def _oracle_hcm(company):
    return oracle_hcm.scrape(company["oracle_hcm"])


def _phenom(company):
    return phenom.scrape(company["phenom"])


def _phenom_widgets(company):
    return phenom_widgets.scrape(company["phenom_widgets"])


def _societegenerale(company):
    return societegenerale.scrape()


def _talentsoft(company):
    return talentsoft.scrape(company["talentsoft"])


def _intervieweb(company):
    return intervieweb.scrape(company["feed_url"])


def _peoplebank(company):
    return peoplebank.scrape(company["category_url"])


def _wp_job(company):
    return wp_job.scrape(company["wp_job"])


def _generic(company):
    return generic.scrape(
        company["url"],
        company.get("selectors", {}),
        company_slug=company.get("name", ""),
        fetch=company.get("fetch", "requests"),
    )


def _guidecom(company):
    return guidecom.scrape(company["guidecom"]["tenant"])


def _icims(company):
    return icims.scrape(company["icims"]["base_url"])


def _koch_avature(company):
    return koch_avature.scrape(company.get("koch_avature", {}))


def _kpmg_us(company):
    return kpmg_us.scrape(company["kpmg_us"])


def _manual(company):
    # "manual" = wanted firm that can't be scraped reliably (see manual_check.py).
    # main()/verify_mode skip these before dispatch, so this is normally never
    # called; registered as an explicit no-op (the old ladder returned [] for it)
    # so it's a known ats rather than triggering the unknown-ats WARN.
    return []


HANDLERS: dict[str, callable] = {
    "greenhouse": _greenhouse,
    "bis": _bis,
    "brassring": _brassring,
    "brassring_hosted": _brassring_hosted,
    "glencore": _glencore,
    "uniper": _uniper,
    "lever": _lever,
    "workday": _workday,
    "workday_playwright": _workday_playwright,
    "ashby": _ashby,
    "smartrecruiters": _smartrecruiters,
    "beesite": _beesite,
    "beesite_sitemap": _beesite_sitemap,
    "bamboohr": _bamboohr,
    "ossiam": _ossiam,
    "generali": _generali,
    "vanlanschot": _vanlanschot,
    "refline": _refline,
    "hr_manager": _hr_manager,
    "rss": _rss,
    "ukg": _ukg,
    "appellia": _appellia,
    "achmea": _achmea,
    "getnoticed": _getnoticed,
    "azimut": _azimut,
    "pfa": _pfa,
    "teamio": _teamio,
    "zoho_recruit": _zoho_recruit,
    "sitemap_jobs": _sitemap_jobs,
    "bpce": _bpce,
    "bnpparibas_paced": _bnpparibas_paced,
    "balyasny": _balyasny,
    "goldman": _goldman,
    "citadel": _citadel,
    "csod": _csod,
    "eib": _eib,
    "breezy": _breezy,
    "janestreet": _janestreet,
    "deshaw": _deshaw,
    "deutscheboerse": _deutscheboerse,
    "directemployers": _directemployers,
    "eightfold": _eightfold,
    "euronext": _euronext,
    "jibe": _jibe,
    "emply": _emply,
    "hibob": _hibob,
    "personio": _personio,
    "pinpoint": _pinpoint,
    "umantis": _umantis,
    "rwe": _rwe,
    "workable": _workable,
    "wellsfargo": _wellsfargo,
    "successfactors": _successfactors,
    "successfactors_api": _successfactors_api,
    "successfactors_classic": _successfactors_classic,
    "successfactors_dwr": _successfactors_dwr,
    "erste_btp": _erste_btp,
    "talentbrew": _talentbrew,
    "talnet": _talnet,
    "teamtailor": _teamtailor,
    "bundesbank": _bundesbank,
    "abnamro": _abnamro,
    "avature": _avature,
    "attrax": _attrax,
    "radancy": _radancy,
    "recruitee": _recruitee,
    "recsolu": _recsolu,
    "kernel": _kernel,
    "talentview": _talentview,
    "eploy": _eploy,
    "adp_careercenter": _adp_careercenter,
    "oracle_hcm": _oracle_hcm,
    "phenom": _phenom,
    "phenom_widgets": _phenom_widgets,
    "societegenerale": _societegenerale,
    "talentsoft": _talentsoft,
    "intervieweb": _intervieweb,
    "peoplebank": _peoplebank,
    "generic": _generic,
    "guidecom": _guidecom,
    "icims": _icims,
    "koch_avature": _koch_avature,
    "kpmg_us": _kpmg_us,
    "mckinsey": _mckinsey,
    "wp_job": _wp_job,
    "manual": _manual,
}
