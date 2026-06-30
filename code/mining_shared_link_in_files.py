#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

SHARE_RE = re.compile(r"https?://(?:chatgpt\.com|chat\.openai\.com)/share/[0-9a-fA-F-]{36}")
USER_AGENT = "github-file-share-link-scanner-1.0"
MAX_PAGES = 10


# ----------------------- utils -----------------------

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


def iso_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
    source: str                 # always "file"
    html_url: Optional[str]
    repo: Optional[str]
    path: Optional[str]
    file_sha: Optional[str]
    size: Optional[int]
    created_at: Optional[str]
    extra: Dict[str, Any]

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


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
    {
      "version": 1,
      "files": {
        "query": "(chatgpt.com/share OR chat.openai.com/share)",
        "next_page": 1,
        "done": false,
        "total_count": null
      }
    }
    """

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {
            "version": 1,
            "files": {
                "query": None,
                "next_page": 1,
                "done": False,
                "total_count": None,
            },
        }
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            self.data = json.load(f)

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def query(self) -> Optional[str]:
        return self.data["files"].get("query")

    def set_query(self, q: str) -> None:
        self.data["files"]["query"] = q

    def next_page(self) -> int:
        return int(self.data["files"].get("next_page", 1))

    def set_next_page(self, n: int) -> None:
        self.data["files"]["next_page"] = int(n)

    def done(self) -> bool:
        return bool(self.data["files"].get("done", False))

    def mark_done(self) -> None:
        self.data["files"]["done"] = True

    def total_count(self) -> Optional[int]:
        v = self.data["files"].get("total_count")
        return int(v) if v is not None else None

    def set_total_count(self, n: int) -> None:
        self.data["files"]["total_count"] = int(n)

    def reset_for_query(self, q: str) -> None:
        self.data["files"] = {
            "query": q,
            "next_page": 1,
            "done": False,
            "total_count": None,
        }


# ----------------------- progress/status -----------------------

class Progress:
    def __init__(self, sample_every_sec: int = 30):
        self.start_ts = time.time()
        self.last_status_ts = 0.0
        self.last_sample_ts = 0.0
        self.sample_every_sec = sample_every_sec

        self.mode = "files"
        self.page = 0
        self.query = ""

        self.items_seen = 0
        self.files_read = 0
        self.hits = 0

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

    def add_occ(self, occ: Occurrence) -> None:
        self.hits += 1
        if len(self.samples) < 200:
            self.samples.append(occ)
        else:
            j = random.randint(0, self.hits - 1)
            if j < len(self.samples):
                self.samples[j] = occ

    def inc_items(self, n: int = 1) -> None:
        self.items_seen += n

    def inc_files_read(self, n: int = 1) -> None:
        self.files_read += n

    def _status(self) -> str:
        elapsed = int(time.time() - self.start_ts)
        reset_in = ""
        if self.rate_reset_epoch:
            reset_in = seconds_to_hhmmss(self.rate_reset_epoch - int(time.time()))
        rem = "-" if self.rate_remaining is None else str(self.rate_remaining)
        return (
            f"mode={self.mode:<5} page={self.page:<2} "
            f"results={self.items_seen:<8} files_read={self.files_read:<8} hits={self.hits:<8} "
            f"rate_rem={rem:<5} reset_in={reset_in:<8} elapsed={seconds_to_hhmmss(elapsed)}"
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
            sys.stderr.write(f"[sample] {ex.share_url} | {ex.repo or ''}:{ex.path or ''} | {ex.html_url or ''}\n")
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
        self._transient_attempt = 0

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
            r = self.s.request(method, url, params=params, headers=headers, timeout=60)
            self.p.set_rate_headers(r.headers)
            self.p.tick()

            if r.status_code in (500, 502, 503, 504):
                attempt = self._transient_attempt + 1
                self._transient_attempt = attempt
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

    def request_text(self, method: str, path: str, params: Optional[dict] = None, accept: Optional[str] = None) -> str:
        url = f"{self.base_url}{path}"
        headers = {}
        if accept:
            headers["Accept"] = accept

        while True:
            r = self.s.request(method, url, params=params, headers=headers, timeout=60)
            self.p.set_rate_headers(r.headers)
            self.p.tick()

            if r.status_code in (500, 502, 503, 504):
                wait = 5
                self.p.log(f"[http] {r.status_code} while requesting text; retry in {wait}s")
                countdown_sleep(wait, prefix="[http] ")
                continue

            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "60"))
                self.p.log(f"[rate] 429 while requesting text; backoff {wait}s")
                countdown_sleep(wait, prefix="[rate] ")
                continue

            if r.status_code == 403:
                txt = (r.text or "").lower()
                if "rate limit" in txt:
                    self._sleep_until_reset(r.headers, reason="rate")
                    continue

            if r.headers.get("X-RateLimit-Remaining") == "0":
                self._sleep_until_reset(r.headers, reason="rate")

            r.raise_for_status()
            return r.text


# ----------------------- file search helpers -----------------------

def file_search_queries(extra_query: str = "") -> List[str]:
    queries = [
        "chat.openai.com/share",
        "chatgpt.com/share",
        
    ]
    if extra_query:
        queries = [f"{q} {extra_query}".strip() for q in queries]
    return queries


def fetch_file_text(
    gh: GitHubClient,
    owner: str,
    repo: str,
    path: str,
    ref: Optional[str] = None,
) -> Optional[str]:
    """
    Read file content using the Contents API.
    Strategy:
    1. Try application/vnd.github.object+json.
    2. Fall back to application/vnd.github.raw+json if needed.
    """
    enc_path = quote(path, safe="/")
    params = {"ref": ref} if ref else None

    # First attempt: JSON with base64 content
    try:
        data = gh.request_json(
            "GET",
            f"/repos/{owner}/{repo}/contents/{enc_path}",
            params=params,
            accept="application/vnd.github.object+json",
        )

        if not isinstance(data, dict):
            return None
        if data.get("type") != "file":
            return None

        encoding = data.get("encoding")
        content = data.get("content") or ""

        if encoding == "base64" and content:
            raw = base64.b64decode(content.encode("utf-8"), validate=False)
            return raw.decode("utf-8", errors="replace")

        if encoding == "none" or not content:
            # Large file or content not included in the response: raw fallback
            pass
        else:
            return None

    except requests.HTTPError as ex:
        resp = getattr(ex, "response", None)
        if resp is not None and resp.status_code == 404:
            return None
    except Exception:
        pass

    # Raw fallback
    try:
        text = gh.request_text(
            "GET",
            f"/repos/{owner}/{repo}/contents/{enc_path}",
            params=params,
            accept="application/vnd.github.raw+json",
        )
        return text
    except requests.HTTPError as ex:
        resp = getattr(ex, "response", None)
        if resp is not None and resp.status_code in (404, 415):
            return None
        return None
    except Exception:
        return None


# ----------------------- scanner -----------------------

def scan_files(
    gh: GitHubClient,
    sink: JsonlSink,
    ckpt: Checkpoint,
    dedupe_set: Optional[set],
    per_page: int,
    extra_query: str = "",
) -> None:
    endpoint = "/search/code"
    queries = file_search_queries(extra_query)

    gh.p.mode = "files"

    for q in queries:
        gh.p.query = q

        ckpt_query = ckpt.query()
        if ckpt_query != q:
            gh.p.log(f"[files] query changed/reset -> {q!r}")
            ckpt.reset_for_query(q)
            ckpt.save()

        if ckpt.done():
            gh.p.log(f"[files] already completed for query {q!r}")
            continue

        probe = gh.request_json("GET", endpoint, params={"q": q, "per_page": 1, "page": 1})
        total_count = int(probe.get("total_count", 0))
        ckpt.set_total_count(total_count)
        ckpt.save()

        start_page = ckpt.next_page()
        gh.p.log(f"[files] scan q={q!r} total_count={total_count} resume_page={start_page}")

        for page in range(start_page, MAX_PAGES + 1):
            gh.p.page = page
            gh.p.tick()

            data = gh.request_json("GET", endpoint, params={"q": q, "per_page": per_page, "page": page})
            items = data.get("items", []) or []

            for it in items:
                gh.p.inc_items(1)

                repo_full = ((it.get("repository") or {}).get("full_name")) or ""
                if "/" not in repo_full:
                    continue
                owner, repo = repo_full.split("/", 1)

                path = it.get("path")
                if not path:
                    continue

                html_url = it.get("html_url")
                file_sha = it.get("sha")
                size = it.get("size")
                score = it.get("score")

                text = fetch_file_text(gh, owner, repo, path)
                gh.p.inc_files_read(1)

                if not text:
                    gh.p.tick()
                    continue

                urls = extract_share_urls(text)
                if not urls:
                    gh.p.tick()
                    continue

                for u in urls:
                    occ = Occurrence(
                        share_url=u,
                        source="file",
                        html_url=html_url,
                        repo=repo_full,
                        path=path,
                        file_sha=file_sha,
                        size=size,
                        created_at=iso_ts(),
                        extra={
                            "page": page,
                            "score": score,
                            "matched_query": q,
                        },
                    )

                    if dedupe_set is not None:
                        key = (
                            occ.share_url,
                            occ.source,
                            occ.repo,
                            occ.path,
                            occ.file_sha,
                            occ.html_url,
                        )
                        if key in dedupe_set:
                            continue
                        dedupe_set.add(key)

                    sink.write(occ.to_json())
                    gh.p.add_occ(occ)
                    gh.p.tick()

            ckpt.set_next_page(page + 1)
            ckpt.save()

            if len(items) < per_page or page >= MAX_PAGES:
                ckpt.mark_done()
                ckpt.save()
                gh.p.log(f"[files] DONE for query {q!r}")
                break


# ----------------------- main -----------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="github_file_share_links.jsonl", help="Output JSONL (append)")
    ap.add_argument("--checkpoint", default="checkpoint_files.json", help="Checkpoint file")
    ap.add_argument("--per-page", type=int, default=100)
    ap.add_argument("--sample-every", type=int, default=30)
    ap.add_argument("--flush-every", type=int, default=50)
    ap.add_argument("--dedupe", action="store_true", help="Streaming dedupe (RAM)")
    ap.add_argument(
        "--extra-query",
        default="",
        help='Extra qualifiers for /search/code, e.g. \'language:Markdown\' or \'path:/docs/\'',
    )
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set the GITHUB_TOKEN environment variable before running this script.")

    p = Progress(sample_every_sec=args.sample_every)
    gh = GitHubClient(token, p)
    sink = JsonlSink(args.out, flush_every=args.flush_every)
    ckpt = Checkpoint(args.checkpoint)
    dedupe_set = set() if args.dedupe else None

    try:
        scan_files(
            gh=gh,
            sink=sink,
            ckpt=ckpt,
            dedupe_set=dedupe_set,
            per_page=args.per_page,
            extra_query=args.extra_query,
        )
    finally:
        sink.close()
        sys.stderr.write("\n")
        sys.stderr.flush()

    p.log(f"[done] wrote/append to {args.out} | checkpoint={args.checkpoint}")


if __name__ == "__main__":
    main()