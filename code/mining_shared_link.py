#!/usr/bin/env python3
"""
Global GitHub search for ChatGPT share links (chatgpt.com/share or chat.openai.com/share)

DAY-BY-DAY with:
- split-on-overflow (if a day window exceeds 1000 results, split window in half recursively)
- page-granular checkpoint (resume from exact page inside a window)
- streaming JSONL output (append while scanning)

Optional comment scanning for Issue/PR items found by search:
- Fetch comments ONLY when the Issue/PR body does NOT already contain a share link (big speed win)
- Optional: skip PR review comments (/pulls/{n}/comments)
- Comment scanning is checkpointed per repo#number so restarts do not refetch.

Defaults:
- --start 2024-01-01
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

SHARE_RE = re.compile(r"https?://(?:chatgpt\.com|chat\.openai\.com)/share/[0-9a-fA-F-]{36}")
USER_AGENT = "global-share-link-scanner/daybyday-comments-2.0"

SEARCH_HARD_CAP = 1000
MAX_PAGES = 10


# ----------------------- small utils -----------------------

def extract_share_urls(text: str) -> List[str]:
    if not text:
        return []
    found = SHARE_RE.findall(text)
    out, seen = [], set()
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def countdown_sleep(total_seconds: int, prefix: str = "[rate] ") -> None:
    if total_seconds <= 0:
        return
    end = time.time() + total_seconds
    while True:
        remaining = int(round(end - time.time()))
        if remaining <= 0:
            break
        sys.stderr.write(f"\r{prefix}sleeping {remaining:>4d}s ")
        sys.stderr.flush()
        time.sleep(1)
    sys.stderr.write("\r" + " " * (len(prefix) + 24) + "\r")
    sys.stderr.flush()


def iso_date(d: dt.date) -> str:
    return d.isoformat()


def iso_dt(t: dt.datetime) -> str:
    # keep it simple (no timezone); GitHub parsing may vary.
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def seconds_to_hhmmss(sec: int) -> str:
    sec = max(0, sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ----------------------- data -----------------------

@dataclass
class Occurrence:
    share_url: str
    source: str                  # "issue"|"pr"|"commit"|"issue_comment"|"pr_comment"|"pr_review_comment"
    html_url: Optional[str]       # parent Issue/PR/Commit URL
    repo: Optional[str]           # owner/name
    number: Optional[int]         # issue/pr number
    commit_sha: Optional[str]
    created_at: Optional[str]

    # comment-specific
    comment_id: Optional[int] = None
    comment_html_url: Optional[str] = None
    comment_type: Optional[str] = None   # "issue_comment"|"pr_review_comment"

    extra: Optional[Dict[str, Any]] = None

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        if d["extra"] is None:
            d["extra"] = {}
        return d


class JsonlSink:
    def __init__(self, path: str, flush_every: int = 50):
        self._f = open(path, "a", encoding="utf-8")
        self.flush_every = flush_every
        self._n = 0

    def write(self, obj: Dict[str, Any]) -> None:
        self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._n += 1
        if self._n % self.flush_every == 0:
            self._f.flush()

    def close(self) -> None:
        self._f.flush()
        self._f.close()


# ----------------------- checkpoint -----------------------

class Checkpoint:
    """
    Cursor-based checkpoint + page-granular windows + comment-scan flags.

    {
      "version": 2,
      "modes": {
        "issues": {
          "cursor_date": "2024-02-01",
          "windows": {
             "<window_key>": {"done": false, "next_page": 4, "total_count": 980}
          },
          "comment_scanned": {
             "owner/repo#123": {"issue_comments": true, "pr_review_comments": true}
          }
        },
        "commits": { ... }
      },
      "capability": {
        "issues_datetime_supported": true/false,
        "commits_datetime_supported": true/false
      }
    }
    """

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {
            "version": 2,
            "modes": {
                "issues": {"cursor_date": None, "windows": {}, "comment_scanned": {}},
                "commits": {"cursor_date": None, "windows": {}, "comment_scanned": {}},
            },
            "capability": {
                "issues_datetime_supported": None,
                "commits_datetime_supported": None,
            },
        }
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        # ensure keys exist
        self.data.setdefault("version", 2)
        self.data.setdefault("modes", {})
        for m in ("issues", "commits"):
            self.data["modes"].setdefault(m, {})
            self.data["modes"][m].setdefault("cursor_date", None)
            self.data["modes"][m].setdefault("windows", {})
            self.data["modes"][m].setdefault("comment_scanned", {})
        self.data.setdefault("capability", {})
        self.data["capability"].setdefault("issues_datetime_supported", None)
        self.data["capability"].setdefault("commits_datetime_supported", None)

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get_cursor_date(self, mode: str) -> Optional[dt.date]:
        s = self.data["modes"][mode]["cursor_date"]
        return dt.date.fromisoformat(s) if s else None

    def set_cursor_date(self, mode: str, d: dt.date) -> None:
        self.data["modes"][mode]["cursor_date"] = iso_date(d)

    # windows
    def windows(self, mode: str) -> Dict[str, Any]:
        return self.data["modes"][mode].setdefault("windows", {})

    def window_get(self, mode: str, key: str) -> Dict[str, Any]:
        return self.windows(mode).get(key, {})

    def window_ensure(self, mode: str, key: str, total_count: Optional[int]) -> None:
        w = self.windows(mode)
        if key not in w:
            w[key] = {"done": False, "next_page": 1, "total_count": total_count}
        else:
            if w[key].get("total_count") is None and total_count is not None:
                w[key]["total_count"] = total_count

    def window_done(self, mode: str, key: str) -> bool:
        return bool(self.window_get(mode, key).get("done", False))

    def window_next_page(self, mode: str, key: str) -> int:
        return int(self.window_get(mode, key).get("next_page", 1))

    def window_set_next_page(self, mode: str, key: str, next_page: int) -> None:
        self.windows(mode).setdefault(key, {"done": False, "next_page": 1, "total_count": None})
        self.windows(mode)[key]["next_page"] = int(next_page)

    def window_mark_done(self, mode: str, key: str) -> None:
        self.windows(mode).setdefault(key, {"done": False, "next_page": 1, "total_count": None})
        self.windows(mode)[key]["done"] = True
        self.windows(mode)[key]["next_page"] = 1

    def clear_windows_for_day(self, mode: str, day: dt.date) -> None:
        w = self.windows(mode)
        prefix = iso_date(day)
        to_del = [k for k in w.keys() if k.startswith(prefix)]
        for k in to_del:
            del w[k]

    # comment scan flags
    def _comment_map(self, mode: str) -> Dict[str, Any]:
        return self.data["modes"][mode].setdefault("comment_scanned", {})

    @staticmethod
    def item_key(repo_full: str, number: int) -> str:
        return f"{repo_full}#{number}"

    def is_comments_scanned(self, mode: str, repo_full: str, number: int, kind: str) -> bool:
        k = self.item_key(repo_full, number)
        return bool(self._comment_map(mode).get(k, {}).get(kind, False))

    def mark_comments_scanned(self, mode: str, repo_full: str, number: int, kind: str) -> None:
        k = self.item_key(repo_full, number)
        self._comment_map(mode).setdefault(k, {})
        self._comment_map(mode)[k][kind] = True


# ----------------------- progress/status -----------------------

class Progress:
    def __init__(self, sample_every_sec: int = 30):
        self.start_ts = time.time()
        self.last_status_ts = 0.0
        self.last_sample_ts = 0.0
        self.sample_every_sec = sample_every_sec

        self.mode = ""
        self.day = ""
        self.window = ""
        self.page = 0

        self.items_seen = 0
        self.hits = 0

        self.comment_calls = 0
        self.comment_hits = 0

        self.req_calls = 0
        self.last_rate_ts = time.time()
        self.req_calls_last = 0

        self.rate_remaining: Optional[int] = None
        self.rate_reset_epoch: Optional[int] = None
        self.samples: List[Occurrence] = []

    def set_rate_headers(self, headers: Dict[str, str]) -> None:
        rem = headers.get("X-RateLimit-Remaining")
        rst = headers.get("X-RateLimit-Reset")
        if rem and rem.isdigit():
            self.rate_remaining = int(rem)
        if rst and rst.isdigit():
            self.rate_reset_epoch = int(rst)

    def add_occ(self, occ: Occurrence, is_comment: bool = False) -> None:
        self.hits += 1
        if is_comment:
            self.comment_hits += 1
        if len(self.samples) < 200:
            self.samples.append(occ)
        else:
            j = random.randint(0, self.hits - 1)
            if j < len(self.samples):
                self.samples[j] = occ

    def inc_items(self, n: int = 1) -> None:
        self.items_seen += n

    def inc_comment_call(self, n: int = 1) -> None:
        self.comment_calls += n

    def inc_req(self, n: int = 1) -> None:
        self.req_calls += n

    def _req_rate(self) -> str:
        now = time.time()
        dt_s = now - self.last_rate_ts
        if dt_s >= 2.0:
            delta = self.req_calls - self.req_calls_last
            self.req_calls_last = self.req_calls
            self.last_rate_ts = now
            self._last_rate = delta / dt_s if dt_s > 0 else 0.0
        rate = getattr(self, "_last_rate", 0.0)
        return f"{rate:.1f}/s"

    def _status(self) -> str:
        elapsed = int(time.time() - self.start_ts)
        reset_in = ""
        if self.rate_reset_epoch:
            reset_in = seconds_to_hhmmss(self.rate_reset_epoch - int(time.time()))
        rem = "-" if self.rate_remaining is None else str(self.rate_remaining)
        return (
            f"mode={self.mode:<7} day={self.day:<10} page={self.page:<2} "
            f"items={self.items_seen:<9} hits={self.hits:<8} "
            f"req={self.req_calls:<7} ({self._req_rate():>6}) "
            f"c_calls={self.comment_calls:<6} c_hits={self.comment_hits:<6} "
            f"rate_rem={rem:<5} reset_in={reset_in:<8} elapsed={seconds_to_hhmmss(elapsed)} "
            f"window={self.window}"
        )

    def tick(self, force: bool = False) -> None:
        now = time.time()
        if force or (now - self.last_status_ts) > 0.2:
            sys.stderr.write("\r" + self._status())
            sys.stderr.flush()
            self.last_status_ts = now

        if self.samples and (now - self.last_sample_ts) >= self.sample_every_sec:
            ex = random.choice(self.samples)
            sys.stderr.write("\n")
            extra = ""
            if ex.comment_html_url:
                extra = f" comment={ex.comment_html_url}"
            sys.stderr.write(f"[sample] {ex.share_url} | {ex.source} {ex.repo or ''} {ex.html_url or ''}{extra}\n")
            sys.stderr.flush()
            self.last_sample_ts = now
            self.tick(force=True)

    def log(self, msg: str) -> None:
        sys.stderr.write("\n" + msg + "\n")
        sys.stderr.flush()
        self.tick(force=True)


# ----------------------- github client -----------------------

class GitHubClient:
    def __init__(self, token: str, p: Progress, base_url: str = "https://api.github.com", buffer_seconds: int = 2):
        self.base_url = base_url.rstrip("/")
        self.p = p
        self.buffer_seconds = buffer_seconds
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            }
        )

    def _sleep_until_reset(self, headers: Dict[str, str], reason: str = "rate") -> None:
        reset = headers.get("X-RateLimit-Reset")
        now = int(time.time())
        if reset and reset.isdigit():
            wait = max(0, int(reset) - now + self.buffer_seconds)
            self.p.log(f"[{reason}] hit limit; sleeping until reset (~{wait}s)")
            countdown_sleep(wait, prefix=f"[{reason}] ")
        else:
            self.p.log(f"[{reason}] hit limit; sleeping fixed 60s (no reset header)")
            countdown_sleep(60, prefix=f"[{reason}] ")

    def request_json(self, method: str, path: str, params: Optional[dict] = None, accept: Optional[str] = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {}
        if accept:
            headers["Accept"] = accept

        while True:
            self.p.inc_req(1)
            r = self.s.request(method, url, params=params, headers=headers, timeout=60)

            self.p.set_rate_headers(r.headers)
            self.p.tick()

            # ---- transient server errors: retry with backoff ----
            if r.status_code in (500, 502, 503, 504):
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                else:
                    attempt = getattr(self, "_transient_attempt", 0) + 1
                    self._transient_attempt = attempt
                    # exponential backoff capped + jitter
                    wait = min(60, 2 ** min(attempt, 6)) + random.randint(0, 3)

                self.p.log(f"[http] {r.status_code} Server Error; retry in {wait}s")
                countdown_sleep(wait, prefix="[http] ")
                continue
            else:
                self._transient_attempt = 0

            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                wait = int(ra) if ra and ra.isdigit() else 60
                self.p.log(f"[rate] 429 Too Many Requests; backoff {wait}s")
                countdown_sleep(wait, prefix="[rate] ")
                continue

            if r.status_code == 403:
                txt = (r.text or "").lower()
                if "rate limit" in txt:
                    self._sleep_until_reset(r.headers, reason="rate")
                    continue
                if "secondary rate limit" in txt or "abuse" in txt:
                    wait = 30 + random.randint(0, 30)
                    self.p.log(f"[rate] secondary limit; backoff {wait}s")
                    countdown_sleep(wait, prefix="[rate] ")
                    continue

            if r.headers.get("X-RateLimit-Remaining") == "0":
                self._sleep_until_reset(r.headers, reason="rate")

            r.raise_for_status()
            return r.json()


# ----------------------- comment endpoints -----------------------

def parse_owner_repo(repo_full: str) -> Tuple[str, str]:
    owner, repo = repo_full.split("/", 1)
    return owner, repo


def list_issue_comments(gh: GitHubClient, repo_full: str, number: int, max_pages: int) -> Iterable[dict]:
    owner, repo = parse_owner_repo(repo_full)
    per_page = 100
    for page in range(1, max_pages + 1):
        gh.p.inc_comment_call(1)
        data = gh.request_json(
            "GET",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            params={"per_page": per_page, "page": page},
        )
        if not data:
            return
        for c in data:
            yield c
        if len(data) < per_page:
            return


def list_pr_review_comments(gh: GitHubClient, repo_full: str, number: int, max_pages: int) -> Iterable[dict]:
    owner, repo = parse_owner_repo(repo_full)
    per_page = 100
    for page in range(1, max_pages + 1):
        gh.p.inc_comment_call(1)
        data = gh.request_json(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{number}/comments",
            params={"per_page": per_page, "page": page},
        )
        if not data:
            return
        for c in data:
            yield c
        if len(data) < per_page:
            return


# ----------------------- query/window helpers -----------------------

def issues_window_query(window: Tuple[dt.datetime, dt.datetime], datetime_supported: bool) -> str:
    base = '(chatgpt.com/share OR chat.openai.com/share) in:body,in:comments'
    if datetime_supported:
        return f"{base} created:{iso_dt(window[0])}..{iso_dt(window[1])}"
    d = window[0].date()
    return f"{base} created:{iso_date(d)}..{iso_date(d)}"


def commits_window_query(window: Tuple[dt.datetime, dt.datetime], date_qualifier: str, datetime_supported: bool) -> str:
    base = '(chatgpt.com/share OR chat.openai.com/share)'
    if datetime_supported:
        return f"{base} {date_qualifier}:{iso_dt(window[0])}..{iso_dt(window[1])}"
    d = window[0].date()
    return f"{base} {date_qualifier}:{iso_date(d)}..{iso_date(d)}"


def window_key(window: Tuple[dt.datetime, dt.datetime], datetime_supported: bool) -> str:
    if datetime_supported:
        return f"{iso_dt(window[0])}..{iso_dt(window[1])}"
    return iso_date(window[0].date())


def split_window(window: Tuple[dt.datetime, dt.datetime]) -> Tuple[Tuple[dt.datetime, dt.datetime], Tuple[dt.datetime, dt.datetime]]:
    start, end = window
    mid = start + (end - start) / 2
    mid = dt.datetime.fromtimestamp(int(mid.timestamp()))
    if mid <= start:
        mid = start + dt.timedelta(seconds=1)
    left = (start, mid)
    right = (mid + dt.timedelta(seconds=1), end)
    return left, right


def test_datetime_support(gh: GitHubClient, endpoint: str, qualifier: str, accept: Optional[str]) -> bool:
    now = dt.datetime.utcnow()
    w = (now - dt.timedelta(minutes=10), now)
    q = f"(test) {qualifier}:{iso_dt(w[0])}..{iso_dt(w[1])}"
    try:
        gh.request_json("GET", endpoint, params={"q": q, "per_page": 1, "page": 1}, accept=accept)
        return True
    except Exception:
        return False


# ----------------------- paging helpers -----------------------

def fetch_pages(
    gh: GitHubClient,
    endpoint: str,
    q: str,
    accept: Optional[str],
    per_page: int,
    start_page: int,
) -> Iterable[Tuple[int, List[dict], int]]:
    total_count = None
    for page in range(start_page, MAX_PAGES + 1):
        gh.p.page = page
        gh.p.tick()
        data = gh.request_json("GET", endpoint, params={"q": q, "per_page": per_page, "page": page}, accept=accept)
        items = data.get("items", []) or []
        if total_count is None:
            total_count = int(data.get("total_count", 0))
        yield page, items, total_count
        if len(items) < per_page:
            return


def maybe_write_occurrence(sink: JsonlSink, dedupe_set: Optional[set], occ: Occurrence) -> bool:
    if dedupe_set is not None:
        key = (occ.share_url, occ.source, occ.repo, occ.number, occ.commit_sha, occ.html_url, occ.comment_id, occ.comment_type)
        if key in dedupe_set:
            return False
        dedupe_set.add(key)
    sink.write(occ.to_json())
    return True


# ----------------------- comments scanning (only when needed) -----------------------

def scan_comments_for_issue_pr(
    gh: GitHubClient,
    sink: JsonlSink,
    ckpt: Checkpoint,
    dedupe_set: Optional[set],
    repo_full: str,
    number: int,
    parent_html_url: str,
    is_pr: bool,
    wkey: str,
    max_comment_pages: int,
    scan_review_comments: bool,
) -> None:
    # issue comments
    if not ckpt.is_comments_scanned("issues", repo_full, number, "issue_comments"):
        for c in list_issue_comments(gh, repo_full, number, max_pages=max_comment_pages):
            cbody = c.get("body") or ""
            urls = extract_share_urls(cbody)
            if not urls:
                continue
            for u in urls:
                occ = Occurrence(
                    share_url=u,
                    source=("pr_comment" if is_pr else "issue_comment"),
                    html_url=parent_html_url,
                    repo=repo_full,
                    number=number,
                    commit_sha=None,
                    created_at=c.get("created_at"),
                    comment_id=c.get("id"),
                    comment_html_url=c.get("html_url"),
                    comment_type="issue_comment",
                    extra={"window": wkey, "parent": parent_html_url},
                )
                if maybe_write_occurrence(sink, dedupe_set, occ):
                    gh.p.add_occ(occ, is_comment=True)
                gh.p.tick()

        ckpt.mark_comments_scanned("issues", repo_full, number, "issue_comments")
        ckpt.save()

    # PR review comments (diff comments)
    if is_pr and scan_review_comments and not ckpt.is_comments_scanned("issues", repo_full, number, "pr_review_comments"):
        for c in list_pr_review_comments(gh, repo_full, number, max_pages=max_comment_pages):
            cbody = c.get("body") or ""
            urls = extract_share_urls(cbody)
            if not urls:
                continue
            for u in urls:
                occ = Occurrence(
                    share_url=u,
                    source="pr_review_comment",
                    html_url=parent_html_url,
                    repo=repo_full,
                    number=number,
                    commit_sha=None,
                    created_at=c.get("created_at"),
                    comment_id=c.get("id"),
                    comment_html_url=c.get("html_url"),
                    comment_type="pr_review_comment",
                    extra={"window": wkey, "parent": parent_html_url},
                )
                if maybe_write_occurrence(sink, dedupe_set, occ):
                    gh.p.add_occ(occ, is_comment=True)
                gh.p.tick()

        ckpt.mark_comments_scanned("issues", repo_full, number, "pr_review_comments")
        ckpt.save()


# ----------------------- scanning windows: issues/pr + optional comments -----------------------

def scan_window_issues(
    gh: GitHubClient,
    sink: JsonlSink,
    ckpt: Checkpoint,
    dedupe_set: Optional[set],
    window: Tuple[dt.datetime, dt.datetime],
    datetime_supported: bool,
    per_page: int,
    include_comments: bool,
    max_comment_pages: int,
    scan_review_comments: bool,
    comments_only_if_body_missing: bool,
) -> None:
    endpoint = "/search/issues"
    accept = None
    q = issues_window_query(window, datetime_supported)
    wkey = window_key(window, datetime_supported)
    gh.p.window = wkey

    # total_count probe
    data = gh.request_json("GET", endpoint, params={"q": q, "per_page": 1, "page": 1}, accept=accept)
    tc = int(data.get("total_count", 0))
    ckpt.window_ensure("issues", wkey, total_count=tc)
    ckpt.save()

    if tc > SEARCH_HARD_CAP:
        if not datetime_supported:
            gh.p.log(f"[warn] issues window {wkey} tc={tc}>1000 but datetime not supported => cannot split further.")
            ckpt.window_mark_done("issues", wkey)
            ckpt.save()
            return
        left, right = split_window(window)
        scan_window_issues(gh, sink, ckpt, dedupe_set, left, datetime_supported, per_page,
                          include_comments, max_comment_pages, scan_review_comments, comments_only_if_body_missing)
        scan_window_issues(gh, sink, ckpt, dedupe_set, right, datetime_supported, per_page,
                          include_comments, max_comment_pages, scan_review_comments, comments_only_if_body_missing)
        ckpt.window_mark_done("issues", wkey)
        ckpt.save()
        return

    if ckpt.window_done("issues", wkey):
        return

    start_page = ckpt.window_next_page("issues", wkey)
    gh.p.log(f"[issues] scan window={wkey} tc={tc} resume_page={start_page}")

    for page, items, _tc in fetch_pages(gh, endpoint, q, accept, per_page, start_page):
        for it in items:
            gh.p.inc_items(1)
            gh.p.tick()

            is_pr = "pull_request" in it
            source = "pr" if is_pr else "issue"
            body = it.get("body") or ""
            urls = extract_share_urls(body)

            repo_full = None
            repourl = it.get("repository_url")
            if repourl and "/repos/" in repourl:
                repo_full = repourl.split("/repos/")[-1]

            number = it.get("number")
            parent_html = it.get("html_url")

            # Save occurrences from body
            for u in urls:
                occ = Occurrence(
                    share_url=u,
                    source=source,
                    html_url=parent_html,
                    repo=repo_full,
                    number=int(number) if isinstance(number, int) else None,
                    commit_sha=None,
                    created_at=it.get("created_at"),
                    extra={"window": wkey, "page": page, "title": it.get("title"), "match": "body"},
                )
                if maybe_write_occurrence(sink, dedupe_set, occ):
                    gh.p.add_occ(occ, is_comment=False)
                gh.p.tick()

            # Comment scanning policy (speed optimization)
            do_comments = include_comments and repo_full and isinstance(number, int) and parent_html
            if do_comments and comments_only_if_body_missing and urls:
                do_comments = False

            if do_comments:
                scan_comments_for_issue_pr(
                    gh=gh,
                    sink=sink,
                    ckpt=ckpt,
                    dedupe_set=dedupe_set,
                    repo_full=repo_full,
                    number=int(number),
                    parent_html_url=parent_html,
                    is_pr=bool(is_pr),
                    wkey=wkey,
                    max_comment_pages=max_comment_pages,
                    scan_review_comments=scan_review_comments,
                )

        ckpt.window_set_next_page("issues", wkey, page + 1)
        ckpt.save()

        if len(items) < per_page or page >= MAX_PAGES:
            ckpt.window_mark_done("issues", wkey)
            ckpt.save()
            gh.p.log(f"[issues] DONE window={wkey}")
            return


# ----------------------- scanning windows: commits (message only) -----------------------

def scan_window_commits(
    gh: GitHubClient,
    sink: JsonlSink,
    ckpt: Checkpoint,
    dedupe_set: Optional[set],
    window: Tuple[dt.datetime, dt.datetime],
    date_qualifier: str,
    datetime_supported: bool,
    per_page: int,
) -> None:
    endpoint = "/search/commits"
    accept = "application/vnd.github.cloak-preview+json"
    q = commits_window_query(window, date_qualifier, datetime_supported)
    wkey = window_key(window, datetime_supported)
    gh.p.window = wkey

    data = gh.request_json("GET", endpoint, params={"q": q, "per_page": 1, "page": 1}, accept=accept)
    tc = int(data.get("total_count", 0))
    ckpt.window_ensure("commits", wkey, total_count=tc)
    ckpt.save()

    if tc > SEARCH_HARD_CAP:
        if not datetime_supported:
            gh.p.log(f"[warn] commits window {wkey} tc={tc}>1000 but datetime not supported => cannot split further.")
            ckpt.window_mark_done("commits", wkey)
            ckpt.save()
            return
        left, right = split_window(window)
        scan_window_commits(gh, sink, ckpt, dedupe_set, left, date_qualifier, datetime_supported, per_page)
        scan_window_commits(gh, sink, ckpt, dedupe_set, right, date_qualifier, datetime_supported, per_page)
        ckpt.window_mark_done("commits", wkey)
        ckpt.save()
        return

    if ckpt.window_done("commits", wkey):
        return

    start_page = ckpt.window_next_page("commits", wkey)
    gh.p.log(f"[commits] scan window={wkey} tc={tc} resume_page={start_page}")

    for page, items, _tc in fetch_pages(gh, endpoint, q, accept, per_page, start_page):
        for it in items:
            gh.p.inc_items(1)
            gh.p.tick()

            msg = ((it.get("commit") or {}).get("message")) or ""
            urls = extract_share_urls(msg)
            if not urls:
                continue

            sha = it.get("sha")
            repo_full = (it.get("repository") or {}).get("full_name")
            html_url = it.get("html_url")
            created_at = (((it.get("commit") or {}).get("committer")) or {}).get("date") \
                         or (((it.get("commit") or {}).get("author")) or {}).get("date")

            for u in urls:
                occ = Occurrence(
                    share_url=u,
                    source="commit",
                    html_url=html_url,
                    repo=repo_full,
                    number=None,
                    commit_sha=sha,
                    created_at=created_at,
                    extra={"window": wkey, "page": page, "message_first_line": msg.splitlines()[0] if msg else ""},
                )
                if maybe_write_occurrence(sink, dedupe_set, occ):
                    gh.p.add_occ(occ, is_comment=False)
                gh.p.tick()

        ckpt.window_set_next_page("commits", wkey, page + 1)
        ckpt.save()

        if len(items) < per_page or page >= MAX_PAGES:
            ckpt.window_mark_done("commits", wkey)
            ckpt.save()
            gh.p.log(f"[commits] DONE window={wkey}")
            return


# ----------------------- day-by-day driver -----------------------

def day_window(day: dt.date) -> Tuple[dt.datetime, dt.datetime]:
    start = dt.datetime(day.year, day.month, day.day, 0, 0, 0)
    end = dt.datetime(day.year, day.month, day.day, 23, 59, 59)
    return start, end


def run_mode_day_by_day(
    mode: str,
    gh: GitHubClient,
    sink: JsonlSink,
    ckpt: Checkpoint,
    dedupe_set: Optional[set],
    start: dt.date,
    end: dt.date,
    per_page: int,
    commit_date_qualifier: str,
    include_comments: bool,
    max_comment_pages: int,
    scan_review_comments: bool,
    comments_only_if_body_missing: bool,
) -> None:
    gh.p.mode = mode
    cursor = ckpt.get_cursor_date(mode) or start
    if cursor < start:
        cursor = start

    gh.p.log(f"[{mode}] starting cursor={iso_date(cursor)} end={iso_date(end)}")

    cap_key = f"{mode}_datetime_supported"
    cap = ckpt.data["capability"].get(cap_key)

    if cap is None:
        gh.p.log(f"[{mode}] probing datetime support...")
        if mode == "issues":
            cap = test_datetime_support(gh, "/search/issues", "created", accept=None)
        else:
            cap = test_datetime_support(gh, "/search/commits", commit_date_qualifier, accept="application/vnd.github.cloak-preview+json")
        ckpt.data["capability"][cap_key] = bool(cap)
        ckpt.save()

    datetime_supported = bool(ckpt.data["capability"][cap_key])
    gh.p.log(f"[{mode}] datetime_supported={datetime_supported}")

    day = cursor
    while day <= end:
        gh.p.day = iso_date(day)
        gh.p.window = ""
        gh.p.page = 0
        gh.p.tick(force=True)

        ckpt.set_cursor_date(mode, day)
        ckpt.save()

        gh.p.log(f"[{mode}] === day {iso_date(day)} ===")
        w = day_window(day)

        if mode == "issues":
            scan_window_issues(
                gh, sink, ckpt, dedupe_set,
                window=w,
                datetime_supported=datetime_supported,
                per_page=per_page,
                include_comments=include_comments,
                max_comment_pages=max_comment_pages,
                scan_review_comments=scan_review_comments,
                comments_only_if_body_missing=comments_only_if_body_missing,
            )
        else:
            scan_window_commits(
                gh, sink, ckpt, dedupe_set,
                window=w,
                date_qualifier=commit_date_qualifier,
                datetime_supported=datetime_supported,
                per_page=per_page,
            )

        ckpt.clear_windows_for_day(mode, day)
        ckpt.save()

        day = day + dt.timedelta(days=1)

    gh.p.log(f"[{mode}] completed up to {iso_date(end)}")


# ----------------------- main -----------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="global_share_links.jsonl", help="Output JSONL (append)")
    ap.add_argument("--checkpoint", default="checkpoint.json", help="Checkpoint file")
    ap.add_argument("--only", default="issues,commits", help="Comma list: issues,commits")

    # START DATE: 2024 by default
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())

    ap.add_argument("--per-page", type=int, default=100)
    ap.add_argument("--commit-date-qualifier", default="committer-date", choices=["committer-date", "author-date"])
    ap.add_argument("--sample-every", type=int, default=30)
    ap.add_argument("--flush-every", type=int, default=50)
    ap.add_argument("--dedupe", action="store_true", help="Streaming dedupe (RAM)")

    # Comments controls
    ap.add_argument("--include-comments", action="store_true", help="Scan issue/pr comments for URLs")
    ap.add_argument("--max-comment-pages", type=int, default=3, help="Max pages to fetch for comments endpoints (100 comments/page)")
    ap.add_argument("--skip-review-comments", action="store_true", help="Do not fetch PR review comments (/pulls/{n}/comments)")
    ap.add_argument(
        "--comments-only-if-body-missing",
        action="store_true",
        default=True,
        help="Fetch comments ONLY when body has no share URL (default: true).",
    )
    ap.add_argument(
        "--comments-even-if-body-has-links",
        action="store_true",
        help="Override: fetch comments even if body already has share URL(s).",
    )

    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set the GITHUB_TOKEN environment variable before running this script.")

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    if start > end:
        raise SystemExit("--start must be <= --end")

    modes = {m.strip().lower() for m in args.only.split(",") if m.strip()}

    # comments policy resolution
    comments_only_if_body_missing = True
    if args.comments_even_if_body_has_links:
        comments_only_if_body_missing = False
    elif args.comments_only_if_body_missing:
        comments_only_if_body_missing = True

    scan_review_comments = not args.skip_review_comments

    p = Progress(sample_every_sec=args.sample_every)
    gh = GitHubClient(token, p)
    sink = JsonlSink(args.out, flush_every=args.flush_every)
    ckpt = Checkpoint(args.checkpoint)
    dedupe_set = set() if args.dedupe else None

    try:
        if "issues" in modes:
            run_mode_day_by_day(
                "issues", gh, sink, ckpt, dedupe_set,
                start, end,
                per_page=args.per_page,
                commit_date_qualifier=args.commit_date_qualifier,
                include_comments=args.include_comments,
                max_comment_pages=args.max_comment_pages,
                scan_review_comments=scan_review_comments,
                comments_only_if_body_missing=comments_only_if_body_missing,
            )
        if "commits" in modes:
            run_mode_day_by_day(
                "commits", gh, sink, ckpt, dedupe_set,
                start, end,
                per_page=args.per_page,
                commit_date_qualifier=args.commit_date_qualifier,
                include_comments=False,
                max_comment_pages=args.max_comment_pages,
                scan_review_comments=scan_review_comments,
                comments_only_if_body_missing=comments_only_if_body_missing,
            )
    finally:
        sink.close()
        sys.stderr.write("\n")
        sys.stderr.flush()

    p.log(f"[done] appended results to {args.out} | checkpoint={args.checkpoint}")


if __name__ == "__main__":
    main()