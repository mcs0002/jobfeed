"""Read and aggregate tagger health/cost telemetry for CLI and web UI."""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS_LOG = os.path.join(ROOT, "tag_runs.jsonl")
RATES = {
    "deepseek-v4-pro": {"hit": (0.022, 0.044), "miss": (0.66, 1.32), "out": (1.98, 3.96)},
    "deepseek-v4-flash": {"hit": (0.007, 0.014), "miss": (0.22, 0.44), "out": (0.66, 1.32)},
}
PEAK_HOURS_UTC = set(range(1, 4)) | set(range(6, 10))


def price(rec: dict, ts: datetime) -> float | None:
    rates = RATES.get(rec.get("model", ""))
    if not rates:
        return None
    i = int(ts.astimezone(timezone.utc).hour in PEAK_HOURS_UTC)
    cached = rec.get("tokens_cached", 0)
    miss = max(rec.get("tokens_in", 0) - cached, 0)
    return (cached * rates["hit"][i] + miss * rates["miss"][i]
            + rec.get("tokens_out", 0) * rates["out"][i]) / 1_000_000


def load(days: int = 14, path: str = RUNS_LOG) -> list[tuple[datetime, dict]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    try:
        with open(path) as fp:
            for line in fp:
                try:
                    rec = json.loads(line)
                    ts = datetime.fromisoformat(rec["ts"])
                except (ValueError, KeyError):
                    continue
                if ts >= cutoff:
                    out.append((ts, rec))
    except OSError:
        pass
    return out


def aggregate(days: int = 14, by_run: bool = True,
              path: str = RUNS_LOG) -> list[dict]:
    groups = defaultdict(list)
    for ts, rec in load(days, path):
        key = rec.get("run_id", "?") if by_run else ts.date().isoformat()
        groups[key].append((ts, rec))
    out = []
    for key, rows in groups.items():
        latest = max(ts for ts, _ in rows)
        total = sum(r.get("jobs_total", 0) for _, r in rows)
        tagged = sum(r.get("jobs_tagged", 0) for _, r in rows)
        tokens_in = sum(r.get("tokens_in", 0) for _, r in rows)
        cached = sum(r.get("tokens_cached", 0) for _, r in rows)
        costs = [price(r, ts) for ts, r in rows]
        flags = sorted({f for _, r in rows for f in
                        ("cli_down", "api_fallback", "api_transport_down") if r.get(f)})
        out.append({"key": key, "ts": latest.isoformat(),
                    "provider": ", ".join(sorted({r.get("provider", "?") for _, r in rows})),
                    "model": ", ".join(sorted({r.get("model", "?") for _, r in rows})),
                    "rubric": ", ".join(sorted({r.get("rubric_version", "legacy") for _, r in rows})),
                    "total": total, "tagged": tagged,
                    "coverage": round(100 * tagged / total, 1) if total else 100.0,
                    "cache_rate": round(100 * cached / tokens_in, 1) if tokens_in else 0.0,
                    "cost": sum(c for c in costs if c is not None),
                    "priced": all(c is not None for c in costs),
                    "latency_s": round(sum(r.get("latency_s", 0) for _, r in rows), 1),
                    "failed_batches": sum(r.get("batches_failed", 0) for _, r in rows),
                    "flags": flags})
    return sorted(out, key=lambda r: r["ts"], reverse=True)
