#!/usr/bin/env python3
"""Fresh Google Trends × Reddit TCM demand collector.

No historical values are read by this program. A run is published only when
both platforms pass access, coverage, and field-level validation.
"""
from __future__ import annotations

import argparse
import calendar
import copy
import datetime as dt
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).resolve().parents[1]
REFS = ROOT / "references"
RUNS = ROOT / "data" / "runs"
REPORTS = ROOT / "reports"
UTC = dt.timezone.utc
REPORT_TZ = ZoneInfo("Asia/Shanghai")


class RunBlocked(RuntimeError):
    def __init__(self, platform: str, reason: str, details: Any = None):
        super().__init__(reason)
        self.platform = platform
        self.reason = reason
        self.details = details


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def dated_report_name(timestamp: str) -> str:
    local_date = dt.datetime.fromisoformat(timestamp).astimezone(REPORT_TZ).date().isoformat()
    return f"report_{local_date}.md"


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def json_lines(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def strip_google_prefix(text: str) -> str:
    text = text.lstrip()
    return text[5:] if text.startswith(")]}'") else text


def load_json_response(response: requests.Response, platform: str) -> Any:
    if response.status_code == 429:
        raise RunBlocked(platform, "rate_limited_429", {"url": response.url})
    if response.status_code >= 400:
        raise RunBlocked(platform, f"http_{response.status_code}", {"url": response.url})
    try:
        return json.loads(strip_google_prefix(response.text))
    except json.JSONDecodeError as exc:
        raise RunBlocked(platform, "invalid_json", {"url": response.url, "sample": response.text[:300]}) from exc


class GoogleTrends:
    EXPLORE = "https://trends.google.com/trends/api/explore"
    MULTILINE = "https://trends.google.com/trends/api/widgetdata/multiline"

    def __init__(self, cfg: Dict[str, Any], raw_dir: Path):
        self.cfg = cfg
        self.raw_dir = raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://trends.google.com/trends/explore",
        })
        self.urls: List[str] = []
        self.failures: List[Dict[str, Any]] = []

    def _get(self, url: str, params: Dict[str, Any], label: str) -> requests.Response:
        last = None
        for attempt in range(1, int(self.cfg["max_retries"]) + 1):
            try:
                response = self.session.get(url, params=params, timeout=45)
                last = {"attempt": attempt, "status": response.status_code, "url": response.url}
                if response.status_code != 429 and response.status_code < 500:
                    return response
            except requests.RequestException as exc:
                last = {"attempt": attempt, "error": repr(exc), "url": url}
            if attempt < int(self.cfg["max_retries"]):
                time.sleep(min(45, 4 * (2 ** (attempt - 1))))
        self.failures.append({"label": label, **(last or {})})
        if last and last.get("status") == 429:
            raise RunBlocked("google", "rate_limited_429", last)
        raise RunBlocked("google", "request_failed", last)

    def timeline(self, terms: List[str], batch_id: int) -> Dict[str, Any]:
        req = {
            "comparisonItem": [
                {"keyword": term, "geo": self.cfg["geo"], "time": self.cfg["timeframe"]}
                for term in terms
            ],
            "category": self.cfg["category"],
            "property": self.cfg["property"],
        }
        params = {"hl": self.cfg["hl"], "tz": str(self.cfg["timezone_minutes"]), "req": json.dumps(req, separators=(",", ":"))}
        explore = self._get(self.EXPLORE, params, f"explore-{batch_id}")
        explore_data = load_json_response(explore, "google")
        dump_json(self.raw_dir / f"batch-{batch_id:03d}-explore.json", explore_data)
        widgets = explore_data.get("widgets", [])
        widget = next((w for w in widgets if w.get("id") == "TIMESERIES"), None)
        if not widget or not widget.get("token"):
            raise RunBlocked("google", "timeseries_widget_missing", {"terms": terms})
        params2 = {
            "hl": self.cfg["hl"],
            "tz": str(self.cfg["timezone_minutes"]),
            "req": json.dumps(widget["request"], separators=(",", ":")),
            "token": widget["token"],
        }
        multi = self._get(self.MULTILINE, params2, f"multiline-{batch_id}")
        data = load_json_response(multi, "google")
        dump_json(self.raw_dir / f"batch-{batch_id:03d}-timeline.json", data)
        self.urls.append("https://trends.google.com/trends/explore?" + urllib.parse.urlencode({
            "date": self.cfg["timeframe"], "geo": self.cfg["geo"], "q": ",".join(terms)
        }))
        timeline = data.get("default", {}).get("timelineData", [])
        if not timeline:
            raise RunBlocked("google", "empty_timeline", {"terms": terms})
        return {"terms": terms, "timeline": timeline}


def complete_week_windows(now: dt.datetime, weeks: int) -> Tuple[dt.datetime, dt.datetime, dt.datetime]:
    current_week_start = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    recent_start = current_week_start - dt.timedelta(weeks=weeks)
    prior_start = recent_start - dt.timedelta(weeks=weeks)
    return prior_start, recent_start, current_week_start


def subtract_calendar_months(value: dt.datetime, months: int) -> dt.datetime:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def parse_point_time(point: Dict[str, Any]) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(point["time"]), tz=UTC)


def mean(values: List[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def collect_google(problems: List[Dict[str, Any]], cfg: Dict[str, Any], run_dir: Path) -> Dict[str, Any]:
    client = GoogleTrends(cfg, run_dir / "google" / "raw")
    anchor = cfg["anchor"]
    batch_size = int(cfg["batch_size"])
    requested = [(p["id"], p["google_terms"][0]) for p in problems]
    batches = [requested[i:i + batch_size] for i in range(0, len(requested), batch_size)]
    momentum_weeks = int(cfg["momentum_weeks"])
    prior_start, recent_start, current_start = complete_week_windows(utc_now(), momentum_weeks)
    rows: Dict[str, Dict[str, Any]] = {}
    manifests = []
    for index, batch in enumerate(batches, start=1):
        terms = [term for _, term in batch]
        if anchor not in terms:
            terms.append(anchor)
        result = client.timeline(terms, index)
        manifests.append({"batch": index, "problem_ids": [pid for pid, _ in batch], "terms": terms, "url": client.urls[-1]})
        values = {term: [] for term in terms}
        recent = {term: [] for term in terms}
        prior = {term: [] for term in terms}
        for point in result["timeline"]:
            vals = point.get("value", [])
            stamp = parse_point_time(point)
            for pos, term in enumerate(terms):
                value = float(vals[pos]) if pos < len(vals) else 0.0
                values[term].append(value)
                if recent_start <= stamp < current_start:
                    recent[term].append(value)
                elif prior_start <= stamp < recent_start:
                    prior[term].append(value)
        anchor_all = mean(values[anchor])
        anchor_recent = mean(recent[anchor])
        anchor_prior = mean(prior[anchor])
        if anchor_all <= 0:
            raise RunBlocked("google", "anchor_zero", {"anchor": anchor, "batch": index})
        for pid, term in batch:
            rows[pid] = {
                "term": term,
                "calibrated": mean(values[term]) / anchor_all,
                "recent_calibrated": mean(recent[term]) / anchor_recent if anchor_recent else 0,
                "prior_calibrated": mean(prior[term]) / anchor_prior if anchor_prior else 0,
                "points": len(values[term]),
                "recent_points": len(recent[term]),
                "prior_points": len(prior[term]),
                "batch": index,
            }
        time.sleep(float(cfg["request_delay_seconds"]))
    total = sum(r["calibrated"] for r in rows.values())
    recent_total = sum(r["recent_calibrated"] for r in rows.values())
    prior_total = sum(r["prior_calibrated"] for r in rows.values())
    for row in rows.values():
        row["share"] = row["calibrated"] / total if total else 0
        recent_share = row["recent_calibrated"] / recent_total if recent_total else 0
        prior_share = row["prior_calibrated"] / prior_total if prior_total else 0
        row["recent_share"] = recent_share
        row["prior_share"] = prior_share
        row["change"] = recent_share / prior_share - 1 if prior_share else None
    ranked = sorted(rows, key=lambda pid: rows[pid]["share"], reverse=True)
    for rank, pid in enumerate(ranked, 1):
        rows[pid]["rank"] = rank
    output = {"rows": rows, "query_batches": manifests, "urls": client.urls, "failures": client.failures, "momentum_weeks": momentum_weeks,
              "windows": {"prior_start": prior_start.isoformat(), "recent_start": recent_start.isoformat(), "current_week_start": current_start.isoformat()}}
    dump_json(run_dir / "google" / "query-batches.json", manifests)
    dump_json(run_dir / "google" / "normalized.json", output)
    return output


def command_json(command: List[str], platform: str, timeout: int = 60) -> Any:
    proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RunBlocked(platform, "command_failed", {"command": command, "returncode": proc.returncode, "stderr": proc.stderr[-1000:]})
    text = proc.stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RunBlocked(platform, "command_returned_invalid_json", {"command": command, "sample": text[:500]}) from exc


def extract_posts(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("posts", "results", "items", "children"):
        val = payload.get(key)
        if isinstance(val, list):
            return [x.get("data", x) if isinstance(x, dict) else x for x in val if isinstance(x, dict)]
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        return extract_posts(data)
    return []


def extract_after(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return None
    if "after" in payload:
        return payload.get("after")
    data = payload.get("data")
    return extract_after(data) if isinstance(data, dict) else None


def first(item: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if item.get(key) is not None:
            return item[key]
    return default


def normalize_post(item: Dict[str, Any], query: str) -> Dict[str, Any]:
    post_id = str(first(item, ("id", "post_id", "name"), "")).replace("t3_", "")
    created = first(item, ("created_utc", "created", "timestamp", "date"))
    if isinstance(created, str):
        try:
            created_ts = dt.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            created_ts = 0
    else:
        created_ts = float(created or 0)
    permalink = first(item, ("permalink", "url", "link"), "")
    if permalink.startswith("/"):
        permalink = "https://www.reddit.com" + permalink
    return {
        "id": post_id,
        "title": str(first(item, ("title",), "")),
        "body": str(first(item, ("selftext", "body", "text", "content"), "")),
        "created_utc": created_ts,
        "subreddit": str(first(item, ("subreddit", "community"), "")),
        "score": max(0, int(first(item, ("score", "ups", "upvotes"), 0) or 0)),
        "comments": max(0, int(first(item, ("num_comments", "comments", "comment_count"), 0) or 0)),
        "url": permalink,
        "query": query,
    }


def reddit_preflight() -> Dict[str, Any]:
    doctor = command_json(["agent-reach", "doctor", "--json"], "reddit")
    status = doctor.get("reddit", {})
    active = status.get("active_backend")
    if active == "OpenCLI":
        return {"backend": "opencli", "doctor": status}
    if shutil.which("rdt"):
        proc = subprocess.run(["rdt", "status"], text=True, capture_output=True, timeout=45)
        combined = proc.stdout + "\n" + proc.stderr
        if proc.returncode == 0 and re.search(r"authenticated:\s*true", combined, re.I):
            return {"backend": "rdt", "doctor": status, "status": combined[-2000:]}
    raise RunBlocked("reddit", "authentication_required", {"doctor": status, "instruction": "Refresh Reddit cookies for rdt-cli or connect OpenCLI to a logged-in Reddit browser session."})


def search_reddit(backend: str, query: str, sort: str, cfg: Dict[str, Any], after: Any = None) -> Any:
    if backend == "opencli":
        return command_json(["opencli", "reddit", "search", query, "-f", "json"], "reddit", timeout=120)
    command = ["rdt", "search", query, "--sort", sort, "--time", cfg["time_filter"], "--limit", str(cfg["results_per_query"]), "--json", "--full-text"]
    if after:
        command += ["--after", str(after)]
    return command_json(command, "reddit", timeout=120)


def p95(values: List[float]) -> float:
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, max(0, math.ceil(0.95 * len(values)) - 1))]


def collect_reddit(problems: List[Dict[str, Any]], cfg: Dict[str, Any], run_dir: Path, reuse_raw: bool = False) -> Dict[str, Any]:
    raw_dir = run_dir / "reddit" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    posts: Dict[str, Dict[str, Any]] = {}
    if reuse_raw:
        previous = json.loads((run_dir / "reddit" / "normalized.json").read_text(encoding="utf-8"))
        backend = previous["backend"]
        query_matrix = json.loads((run_dir / "reddit" / "query-matrix.json").read_text(encoding="utf-8"))
    else:
        access = reddit_preflight()
        backend = access["backend"]
        query_matrix = []
    collected_at = dt.datetime.fromisoformat(previous["main_window"]["collected_at"]) if reuse_raw else utc_now()
    cutoff = subtract_calendar_months(collected_at, int(cfg["window_months"]))
    cutoff_ts = cutoff.timestamp()
    # Build one TCM corpus first. Reddit search interprets long Boolean expressions
    # loosely, so exact context-family searches plus local problem classification
    # are more auditable than one giant (TCM terms) AND (problem terms) expression.
    if reuse_raw:
        for raw_path in sorted(raw_dir.glob("*.json")):
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
            for item in extract_posts(payload):
                row = normalize_post(item, raw_path.name)
                if row["id"]:
                    posts.setdefault(row["id"], row)
    else:
        context_queries = cfg["tcm_context_terms"] if len(problems) > 5 else ["traditional Chinese medicine", "acupuncture", "Chinese herbs", "gua sha"]
        for idx, context in enumerate(context_queries, 1):
            query = f'"{context}"' if " " in context else context
            for sort in cfg["sorts"]:
                max_pages = 1 if backend == "opencli" else int(cfg["max_pages_new"] if sort == "new" else cfg["max_pages_top"])
                after = None
                for page in range(1, max_pages + 1):
                    payload = search_reddit(backend, query, sort, cfg, after)
                    safe = re.sub(r"[^a-z0-9]+", "-", context.lower()).strip("-")
                    dump_json(raw_dir / f"{idx:03d}-{safe}-{sort}-p{page:02d}.json", payload)
                    found = extract_posts(payload)
                    normalized_page = [normalize_post(item, query) for item in found]
                    query_matrix.append({"context_term": context, "query": query, "sort": sort, "page": page, "returned": len(found), "backend": backend, "after": after})
                    for row in normalized_page:
                        if not row["id"]:
                            continue
                        existing = posts.setdefault(row["id"], row)
                        existing.setdefault("matched_context_queries", [])
                        if context not in existing["matched_context_queries"]:
                            existing["matched_context_queries"].append(context)
                    next_after = extract_after(payload)
                    page_times = [row["created_utc"] for row in normalized_page if row["created_utc"]]
                    time.sleep(1.0)
                    if not next_after or (sort == "new" and page_times and min(page_times) < cutoff_ts):
                        break
                    after = next_after
    tcm_parts = []
    for term in cfg["tcm_context_terms"]:
        if term == "TCM":
            continue  # ambiguous acronym; search discovery is allowed, eligibility is not
        escaped = re.escape(term)
        tcm_parts.append(r"\b" + escaped + r"\b" if re.fullmatch(r"[A-Za-z ]+", term) else escaped)
    tcm_rx = re.compile("|".join(tcm_parts), re.I)
    patterns = {
        p["id"]: re.compile(
            "|".join(r"(?<!\w)" + re.escape(term) + r"(?!\w)" for term in p["reddit_patterns"]),
            re.I,
        )
        for p in problems
    }
    demand_rx = re.compile(r"\b(i|i'm|i've|my|me|we|our|help|advice|symptoms?|suffer(?:ing)?|does anyone|what can|how can|can't|cannot)\b|\?", re.I)
    excluded_subreddit_rx = re.compile("|".join(cfg.get("excluded_subreddit_patterns", [])), re.I) if cfg.get("excluded_subreddit_patterns") else None
    excluded = []
    eligible = []
    for row in posts.values():
        text = row["title"] + "\n" + row["body"]
        lead = (row["title"] + "\n" + row["body"][:800])
        tcm_spans = [m.span() for m in tcm_rx.finditer(text)]
        labels = []
        for pid, rx in patterns.items():
            problem_spans = [m.span() for m in rx.finditer(text)]
            if any(abs(ps[0] - ts[0]) <= 400 for ps in problem_spans for ts in tcm_spans):
                labels.append(pid)
        tcm_central = bool(tcm_spans)
        reason = None
        if row["created_utc"] and row["created_utc"] < cutoff_ts:
            reason = "outside_window"
        elif not tcm_central:
            reason = "tcm_context_not_central"
        elif not labels:
            reason = "missing_problem"
        elif excluded_subreddit_rx and excluded_subreddit_rx.search(row["subreddit"]):
            reason = "excluded_entertainment_context"
        elif cfg.get("require_personal_demand_signal") and not demand_rx.search(lead):
            reason = "missing_personal_demand_signal"
        elif any(x.lower() in text.lower() for x in cfg["excluded_substrings"]):
            reason = "excluded_context"
        if reason:
            excluded.append({**row, "exclusion_reason": reason})
        else:
            eligible.append({**row, "problem_ids": labels})
    score_cap = p95([p["score"] for p in eligible])
    comment_cap = p95([p["comments"] for p in eligible])
    for p in eligible:
        sc = math.log1p(min(p["score"], score_cap)) / math.log1p(score_cap) if score_cap else 0
        cc = math.log1p(min(p["comments"], comment_cap)) / math.log1p(comment_cap) if comment_cap else 0
        p["heat"] = 0.5 * sc + 0.5 * cc
    momentum_weeks = int(cfg["momentum_weeks"])
    prior_start, recent_start, current_start = complete_week_windows(collected_at, momentum_weeks)
    period = {"all": {}, "recent": {}, "prior": {}}
    counts = {"all": {}, "recent": {}, "prior": {}}
    for p in eligible:
        stamp = dt.datetime.fromtimestamp(p["created_utc"], UTC) if p["created_utc"] else None
        for pid in p["problem_ids"]:
            period["all"][pid] = period["all"].get(pid, 0) + p["heat"]
            counts["all"][pid] = counts["all"].get(pid, 0) + 1
            key = "recent" if stamp and recent_start <= stamp < current_start else "prior" if stamp and prior_start <= stamp < recent_start else None
            if key:
                period[key][pid] = period[key].get(pid, 0) + p["heat"]
                counts[key][pid] = counts[key].get(pid, 0) + 1
    totals = {k: sum(v.values()) for k, v in period.items()}
    rows = {}
    for p in problems:
        pid = p["id"]
        share = period["all"].get(pid, 0) / totals["all"] if totals["all"] else 0
        rs = period["recent"].get(pid, 0) / totals["recent"] if totals["recent"] else 0
        ps = period["prior"].get(pid, 0) / totals["prior"] if totals["prior"] else 0
        rows[pid] = {"share": share, "recent_share": rs, "prior_share": ps, "change": rs / ps - 1 if ps else None,
                     "matches": counts["all"].get(pid, 0), "recent_matches": counts["recent"].get(pid, 0), "prior_matches": counts["prior"].get(pid, 0)}
    for rank, pid in enumerate(sorted(rows, key=lambda x: rows[x]["share"], reverse=True), 1):
        rows[pid]["rank"] = rank
    json_lines(run_dir / "reddit" / "normalized-threads.jsonl", eligible)
    json_lines(run_dir / "reddit" / "excluded-threads.jsonl", excluded)
    dump_json(run_dir / "reddit" / "query-matrix.json", query_matrix)
    output = {"rows": rows, "eligible_posts": len(eligible), "unique_posts": len(posts), "excluded_posts": len(excluded), "score_p95": score_cap,
              "comment_p95": comment_cap, "backend": backend, "query_matrix": query_matrix, "windows": {"prior_start": prior_start.isoformat(), "recent_start": recent_start.isoformat(), "current_week_start": current_start.isoformat()},
              "main_window": {"months": int(cfg["window_months"]), "cutoff": cutoff.isoformat(), "collected_at": collected_at.isoformat()}, "momentum_weeks": momentum_weeks,
              "representative_links": [p["url"] for p in sorted(eligible, key=lambda x: x["heat"], reverse=True) if p["url"]][:20]}
    dump_json(run_dir / "reddit" / "normalized.json", output)
    return output


def fmt_pct(value: Any, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.1%}" if signed else f"{value:.2%}"


def rankdata(values: List[float]) -> List[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j + 2) / 2
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(a: List[float], b: List[float]) -> float:
    if len(a) < 2:
        return float("nan")
    ma, mb = mean(a), mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else float("nan")


def build_report(problems: List[Dict[str, Any]], google: Dict[str, Any], reddit: Dict[str, Any], run_started: str, smoke: bool) -> str:
    by_id = {p["id"]: p for p in problems}
    g, r = google["rows"], reddit["rows"]
    ids = [p["id"] for p in problems]
    rho = pearson(rankdata([g[x]["rank"] for x in ids]), rankdata([r[x]["rank"] for x in ids]))
    g10 = {x for x in ids if g[x]["rank"] <= 10}; r10 = {x for x in ids if r[x]["rank"] <= 10}
    g20 = {x for x in ids if g[x]["rank"] <= 20}; r20 = {x for x in ids if r[x]["rank"] <= 20}
    shared = sorted(g20 & r20, key=lambda x: g[x]["rank"])
    gh = [x for x in ids if g[x]["rank"] <= 20 and r[x]["rank"] - g[x]["rank"] >= 10]
    rh = [x for x in ids if r[x]["rank"] <= 20 and g[x]["rank"] - r[x]["rank"] >= 10]
    label = lambda xs: "、".join(by_id[x]["label_zh"] for x in xs) or "无"
    mode = "烟雾测试（非生产排名）" if smoke else "完整运行"
    momentum_weeks = int(google["momentum_weeks"])
    out = [f"# Google × Reddit 中医问题需求对比（{mode}）", "", f"> 本次采集开始：{run_started} ｜ Google：全球英文网页搜索，滚动最近3个月 ｜ Reddit：英文中医相关帖子，滚动最近3个月", "",
           "## 摘要", "", f"- 双平台 Top 20 共同热点：{label(shared)}。", f"- Google 明显更热（Google Top 20 且领先 Reddit 至少10名）：{label(gh)}。",
           f"- Reddit 明显更热（Reddit Top 20 且领先 Google 至少10名）：{label(rh)}。", f"- Spearman 排名相关系数：`{rho:.3f}`；Top 10 重合 {len(g10&r10)} 项，Top 20 重合 {len(g20&r20)} 项。", "",
           "## 1. 问题大类及子类", ""]
    cats: Dict[str, List[str]] = {}
    for p in problems: cats.setdefault(p["category_zh"], []).append(p["id"])
    cat_rows = []
    for cat, members in cats.items():
        gs = sum(g[x]["share"] for x in members); rs = sum(r[x]["share"] for x in members)
        gr = sum(g[x]["recent_share"] for x in members); gp = sum(g[x]["prior_share"] for x in members)
        rr = sum(r[x]["recent_share"] for x in members); rp = sum(r[x]["prior_share"] for x in members)
        cat_rows.append((cat, members, gs, gr/gp-1 if gp else None, rs, rr/rp-1 if rp else None))
    for cat, members, gs, gc, rs, rc in sorted(cat_rows, key=lambda x: x[2], reverse=True):
        out += [f"### {cat}", "", f"| 层级 | Google 搜索声量 | Google 近{momentum_weeks}周环比 | Reddit 热度声量 | Reddit 近{momentum_weeks}周环比 |", "|---|---:|---:|---:|---:|",
                f"| **大类合计** | **{fmt_pct(gs)}** | **{fmt_pct(gc,True)}** | **{fmt_pct(rs)}** | **{fmt_pct(rc,True)}** |"]
        for pid in sorted(members, key=lambda x: g[x]["rank"]):
            out.append(f"| {by_id[pid]['label_zh']} | {fmt_pct(g[pid]['share'])} | {fmt_pct(g[pid]['change'],True)} | {fmt_pct(r[pid]['share'])} | {fmt_pct(r[pid]['change'],True)} |")
        out.append("")
    out += ["## 2. 具体问题与症状：热度总榜", "", f"| 具体问题/症状 | Google 排名 | Google 搜索声量 | Google 近{momentum_weeks}周环比 | Reddit 排名 | Reddit 热度声量 | Reddit 近{momentum_weeks}周环比 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for pid in sorted(ids, key=lambda x: g[x]["rank"]):
        low = "†" if r[pid]["recent_matches"] + r[pid]["prior_matches"] < 5 else ""
        out.append(f"| {by_id[pid]['label_zh']} | {g[pid]['rank']} | {fmt_pct(g[pid]['share'])} | {fmt_pct(g[pid]['change'],True)} | {r[pid]['rank']} | {fmt_pct(r[pid]['share'])} | {fmt_pct(r[pid]['change'],True)}{low} |")
    out += ["", "## 采集与校验", "", f"- Google 查询批次：{len(google['query_batches'])}；失败：{len(google['failures'])}。",
            f"- Reddit 后端：{reddit['backend']}；唯一帖子 {reddit['unique_posts']}；符合条件 {reddit['eligible_posts']}；排除 {reddit['excluded_posts']}。",
            f"- Reddit 本轮 P95：score={reddit['score_p95']}，comments={reddit['comment_p95']}。", f"- `†`：两段{momentum_weeks}周窗口合计不足5个匹配帖子，环比仅作线索。", "",
            "## 审计链接", ""]
    out += [f"- [Google Trends 批次 {i+1}]({url})" for i, url in enumerate(google["urls"][:12])]
    out += [f"- [Reddit 示例帖子 {i+1}]({url})" for i, url in enumerate(reddit["representative_links"][:12])]
    out += ["", "## 限制", "", "Google Trends 是0–100相对兴趣，批次通过共同锚点校准，并非绝对搜索次数。Reddit热度是本轮合格帖子在所选问题集合中的互动权重占比；Reddit score 是净投票近似值。两端声量只能比较各自结构，不能相加，也不代表疾病患病率。", ""]
    return "\n".join(out)


def validate(google: Dict[str, Any], reddit: Dict[str, Any], problems: List[Dict[str, Any]], cfg: Dict[str, Any], smoke: bool) -> Dict[str, Any]:
    g_rows = google["rows"]
    zero_rate = sum(1 for p in problems if g_rows[p["id"]]["calibrated"] == 0) / len(problems)
    min_points = min(g_rows[p["id"]]["points"] for p in problems)
    min_posts = cfg["validation"]["min_reddit_posts_smoke" if smoke else "min_reddit_posts_full"]
    checks = {
        "google_zero_rate": zero_rate,
        "google_zero_rate_ok": zero_rate <= cfg["validation"]["max_google_zero_rate"],
        "google_min_points": min_points,
        "google_points_ok": min_points >= cfg["validation"]["min_google_points"],
        "reddit_eligible_posts": reddit["eligible_posts"],
        "reddit_posts_ok": reddit["eligible_posts"] >= min_posts,
        "all_required_fields_ok": all("rank" in g_rows[p["id"]] and "rank" in reddit["rows"][p["id"]] for p in problems),
    }
    checks["passed"] = all(v for k, v in checks.items() if k.endswith("_ok"))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true", help="Check live Google Trends and Reddit authentication without publishing")
    parser.add_argument("--smoke", action="store_true", help="Run the small live taxonomy; never publish as the production latest report")
    parser.add_argument("--reprocess-run", help="Recalculate one existing run from its saved fresh raw responses without fetching again")
    args = parser.parse_args()
    config = json.loads((REFS / "config.json").read_text(encoding="utf-8"))
    taxonomy = json.loads((REFS / "taxonomy.json").read_text(encoding="utf-8"))
    problems = taxonomy["problems"]
    if args.reprocess_run:
        run_dir = Path(args.reprocess_run).resolve()
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        google = json.loads((run_dir / "google" / "normalized.json").read_text(encoding="utf-8"))
        reddit = collect_reddit(problems, config["reddit"], run_dir, reuse_raw=True)
        checks = validate(google, reddit, problems, config, False)
        dump_json(run_dir / "validation.json", checks)
        if not checks["passed"]:
            raise RunBlocked("validation", "quality_gate_failed_after_reprocess", checks)
        report = build_report(problems, google, reddit, manifest["started_at"], False)
        REPORTS.mkdir(parents=True, exist_ok=True)
        report_path = REPORTS / dated_report_name(manifest["started_at"])
        report_path.write_text(report, encoding="utf-8")
        manifest.update({"status": "completed", "reprocessed_at": utc_now().isoformat(), "reprocess_reason": "reddit_full_term_boundary_fix", "report": str(report_path)})
        manifest["taxonomy_version"] = taxonomy["version"]
        dump_json(run_dir / "taxonomy.json", taxonomy)
        dump_json(manifest_path, manifest)
        print(f"Reprocessed: {report_path}")
        return 0
    if args.smoke or args.preflight:
        problems = [p for p in problems if p.get("smoke_test")]
    started = utc_now()
    run_id = started.strftime("%Y-%m-%dT%H%M%SZ") + ("-preflight" if args.preflight else "-smoke" if args.smoke else "")
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest = {"run_id": run_id, "started_at": started.isoformat(), "mode": "preflight" if args.preflight else "smoke" if args.smoke else "full",
                "taxonomy_version": taxonomy["version"], "problem_count": len(problems), "config": copy.deepcopy(config), "status": "running"}
    dump_json(run_dir / "manifest.json", manifest)
    dump_json(run_dir / "taxonomy.json", {**taxonomy, "problems": problems})
    try:
        if args.preflight:
            results, failures = {}, []
            try:
                google = collect_google(problems[:1], config["google"], run_dir)
                results["google"] = {"ok": True, "points": next(iter(google["rows"].values()))["points"]}
            except RunBlocked as exc:
                results["google"] = {"ok": False, "reason": exc.reason, "details": exc.details}
                failures.append({"platform": exc.platform, "reason": exc.reason, "details": exc.details})
            try:
                reddit = reddit_preflight()
                results["reddit"] = {"ok": True, "backend": reddit["backend"]}
            except RunBlocked as exc:
                results["reddit"] = {"ok": False, "reason": exc.reason, "details": exc.details}
                failures.append({"platform": exc.platform, "reason": exc.reason, "details": exc.details})
            dump_json(run_dir / "preflight.json", results)
            if failures:
                raise RunBlocked("preflight", "one_or_more_platforms_unavailable", failures)
            manifest.update({"status": "preflight_passed", "finished_at": utc_now().isoformat()})
            dump_json(run_dir / "manifest.json", manifest)
            print(f"Preflight passed: {run_dir}")
            return 0
        google = collect_google(problems, config["google"], run_dir)
        reddit = collect_reddit(problems, config["reddit"], run_dir)
        checks = validate(google, reddit, problems, config, args.smoke)
        dump_json(run_dir / "validation.json", checks)
        if not checks["passed"]:
            raise RunBlocked("validation", "quality_gate_failed", checks)
        report = build_report(problems, google, reddit, started.isoformat(), args.smoke)
        report_name = dated_report_name(started.isoformat())
        report_path = run_dir / report_name if args.smoke else REPORTS / report_name
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        manifest.update({"status": "completed", "finished_at": utc_now().isoformat(), "report": str(report_path)})
        dump_json(run_dir / "manifest.json", manifest)
        print(f"Completed: {report_path}")
        return 0
    except RunBlocked as exc:
        failure = {"platform": exc.platform, "reason": exc.reason, "details": exc.details, "failed_at": utc_now().isoformat()}
        dump_json(run_dir / "failure.json", failure)
        manifest.update({"status": "blocked", "finished_at": utc_now().isoformat(), "failure": failure})
        dump_json(run_dir / "manifest.json", manifest)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except Exception as exc:
        failure = {"platform": "runtime", "reason": type(exc).__name__, "details": repr(exc), "failed_at": utc_now().isoformat()}
        dump_json(run_dir / "failure.json", failure)
        manifest.update({"status": "failed", "finished_at": utc_now().isoformat(), "failure": failure})
        dump_json(run_dir / "manifest.json", manifest)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
