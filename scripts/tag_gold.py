#!/usr/bin/env python3
"""Score one configured tagger against the versioned independent gold set.

The gold file contains the exact description excerpt shown to the adjudicator.
This script sends that same evidence to the configured API, disables every
fallback and production log, and scores the final post-processed tags.

Example on the M1:

    TAG_PROVIDER=api \
    TAG_API_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
    TAG_API_MODEL=gemini-2.5-flash \
    TAG_API_FORMAT=json_rows \
    TAG_API_EXTRA='{"reasoning_effort":"none"}' \
    TAG_RUBRIC_ADDENDUM=exclusions \
    .venv/bin/python scripts/tag_gold.py --api-key-var TAG_AB_API_KEY

The optional key-variable argument promotes that key to `TAG_API_KEY` for this
process only; without it, the configured production key is used.
"""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tag  # noqa: E402


FIELDS = ("area", "desk", "seniority", "job_type", "loc_country", "work_mode")


def _norm(value) -> str:
    return "" if value in (None, "-") else str(value)


def _load_gold(path: str) -> list[dict]:
    rows = [json.loads(line) for line in Path(path).read_text().splitlines()
            if line.strip()]
    ids = [row.get("gold_id") for row in rows]
    if not rows or len(ids) != len(set(ids)):
        raise ValueError("gold set is empty or has duplicate gold_id values")
    for row in rows:
        if not all(k in row for k in ("gold_id", "description", "gold",
                                      "confidence")):
            raise ValueError(f"malformed gold row {row.get('gold_id')!r}")
        if not all(field in row["gold"] for field in FIELDS):
            raise ValueError(f"incomplete labels for {row['gold_id']}")
    return rows


def _binary(gold: list[dict], jobs: list[dict], predicate) -> dict:
    tp = fp = fn = tn = 0
    for row, job in zip(gold, jobs):
        actual, guessed = predicate(row["gold"]), predicate(job)
        if actual and guessed:
            tp += 1
        elif guessed:
            fp += 1
        elif actual:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall}


def score(gold: list[dict], jobs: list[dict]) -> dict:
    high = [i for i, row in enumerate(gold) if row["confidence"] == "high"]

    def acc(field: str, indices: list[int]) -> float:
        return sum(_norm(gold[i]["gold"][field]) == _norm(jobs[i].get(field))
                   for i in indices) / len(indices)

    all_indices = list(range(len(gold)))
    exact = lambda indices: sum(
        all(_norm(gold[i]["gold"][field]) == _norm(jobs[i].get(field))
            for field in FIELDS) for i in indices
    ) / len(indices)
    return {
        "n": len(gold),
        "n_high_confidence": len(high),
        "accuracy": {field: acc(field, all_indices) for field in FIELDS},
        "accuracy_high_confidence": {field: acc(field, high) for field in FIELDS},
        "joint_exact": exact(all_indices),
        "joint_exact_high_confidence": exact(high),
        "finance_inclusion": _binary(
            gold, jobs, lambda row: _norm(row.get("area")) != "other"),
        "default_visibility": _binary(
            gold, jobs,
            lambda row: (_norm(row.get("area")) != "other"
                         and _norm(row.get("seniority")) != "manager")),
    }


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(root / "evals" / "tagger_gold_v1.jsonl"))
    ap.add_argument("--api-key-var",
                    help="alternate .env variable to use for this run")
    ap.add_argument("--predictions-out")
    ap.add_argument("--report-out")
    args = ap.parse_args()

    if tag._provider() != "api":
        print("TAG_PROVIDER must select the API transport.", file=sys.stderr)
        return 2
    if args.api_key_var:
        candidate_key = tag._cfg(args.api_key_var)
        if not candidate_key:
            print(f"{args.api_key_var} is empty or missing.", file=sys.stderr)
            return 2
        os.environ["TAG_API_KEY"] = candidate_key
    cfg = tag._openai_cfg()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        print("TAG_API_BASE_URL / _API_KEY / _MODEL must all be set.",
              file=sys.stderr)
        return 2

    gold = _load_gold(args.gold)
    jobs = [{
        "gold_id": row["gold_id"],
        "company": row["company"],
        "title": row["title"],
        "location": row["location"],
        "category": row["sector"],
        "description": row["description"],
    } for row in gold]

    tag._claude_bin = lambda: None
    tag._api_key = lambda: ""
    tag._record_run = lambda health: None
    tag._log_debug = lambda *parts: None
    tag._desc_excerpt = lambda job: job.get("description") or ""
    tag.tag_jobs(jobs)

    health = dict(tag.LAST_RUN_HEALTH)
    retries = health.get("subbatch_retries", 0) + health.get("single_retries", 0)
    clean = (health.get("jobs_tagged") == len(jobs)
             and health.get("batches_failed", 0) == 0
             and retries == 0
             and not health.get("api_fallback")
             and not health.get("api_transport_down")
             and not health.get("cli_down"))
    report = {
        "clean": clean,
        "provenance": tag.tag_provenance(),
        "health": health,
        "score": score(gold, jobs),
    }

    if args.predictions_out:
        with Path(args.predictions_out).open("w") as fp:
            for job in jobs:
                rec = {"gold_id": job["gold_id"]}
                rec.update({field: job.get(field) for field in tag.TAG_KEYS})
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report_out:
        Path(args.report_out).write_text(rendered + "\n")
    print(rendered)
    return 0 if clean else 3


if __name__ == "__main__":
    raise SystemExit(main())
