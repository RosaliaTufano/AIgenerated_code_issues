#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set

from playwright.async_api import async_playwright, Browser, Page
from tqdm import tqdm


def _clean_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\r\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


async def _wait_for_conversation_stability(
    page: Page,
    base_wait_ms: int = 2500,
    max_rounds: int = 20,
    round_wait_ms: int = 500,
    stable_rounds_needed: int = 3,
) -> Dict[str, Any]:
    """
    Wait until the page becomes stable.
    The conversation is considered stable when:
    - at least one [data-message-author-role] node exists;
    - for N consecutive rounds, the following values remain unchanged:
      - number of messages;
      - number of <pre> elements;
      - body text length.
    """
    await page.wait_for_timeout(base_wait_ms)

    try:
        await page.wait_for_selector("[data-message-author-role]", timeout=10_000)
    except Exception:
        return {
            "ok": False,
            "reason": "Timeout waiting for [data-message-author-role]",
            "final_counts": None,
        }

    try:
        await page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass

    prev = None
    stable_rounds = 0
    final_counts = None

    for _ in range(max_rounds):
        counts = await page.evaluate(
            """
            () => {
              const msgNodes = Array.from(document.querySelectorAll('[data-message-author-role]'));

              const msgCount = msgNodes.length;
              const preCount = document.querySelectorAll('pre').length;
              const bodyTextLen = (document.body?.innerText || '').length;

              const perMessage = msgNodes.map((node) => {
                const role = node.getAttribute('data-message-author-role') || 'unknown';
                const textLen = (node.innerText || node.textContent || '').length;
                const localPreCount = node.querySelectorAll('pre').length;
                return { role, textLen, localPreCount };
              });

              return {
                msgCount,
                preCount,
                bodyTextLen,
                perMessage
              };
            }
            """
        )

        final_counts = counts

        if counts == prev:
            stable_rounds += 1
        else:
            stable_rounds = 0
            prev = counts

        if stable_rounds >= stable_rounds_needed:
            return {
                "ok": True,
                "reason": "stable",
                "final_counts": final_counts,
            }

        await page.wait_for_timeout(round_wait_ms)

    return {
        "ok": True,
        "reason": "max_rounds_reached",
        "final_counts": final_counts,
    }


async def reconstruct_conversation_with_inline_code_list_async(
    browser: Browser,
    url: str,
    wait_ms: int = 2500,
    token_prefix: str = "CODE",
    debug: bool = False,
) -> Dict[str, Any]:
    page = await browser.new_page()
    status: Optional[int] = None
    payload = None
    ok = True
    reason = "OK"
    diagnostics: Dict[str, Any] = {}

    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=50_000)
        status = resp.status if resp else None

        stability = await _wait_for_conversation_stability(
            page=page,
            base_wait_ms=wait_ms,
            max_rounds=20,
            round_wait_ms=500,
            stable_rounds_needed=3,
        )
        diagnostics["stability"] = stability

        payload = await page.evaluate(
            """
            (tokenPrefix) => {
              const out = [];
              const nodes = Array.from(document.querySelectorAll('[data-message-author-role]'));

              function getCodeLanguage(pre) {
                const candidates = [
                  pre,
                  pre.querySelector('code'),
                  pre.parentElement,
                  pre.previousElementSibling,
                  pre.closest('[class]'),
                ].filter(Boolean);

                for (const el of candidates) {
                  const cls = el.getAttribute && el.getAttribute('class');
                  if (!cls) continue;

                  const m = cls.match(/(?:language|lang)[-_:]([a-zA-Z0-9_+.-]+)/i);
                  if (m) return m[1];
                }

                return '';
              }

              function walk(node, parts, placeholders, codeBlocks) {
                if (!node) return;

                if (node.nodeType === Node.TEXT_NODE) {
                  parts.push(node.nodeValue || '');
                  return;
                }

                if (node.nodeType !== Node.ELEMENT_NODE) {
                  return;
                }

                const tag = (node.tagName || '').toLowerCase();

                if (tag === 'pre') {
                  const idx = codeBlocks.length;
                  const ph = `__${tokenPrefix}_PLACEHOLDER_${idx}__`;

                  const codeEl = node.querySelector('code');
                  const raw = codeEl
                    ? (codeEl.innerText || codeEl.textContent || '')
                    : (node.innerText || node.textContent || '');

                  const lang = getCodeLanguage(node);

                  placeholders.push(ph);
                  codeBlocks.push({
                    language: lang,
                    code: (raw || '').replace(/\\r\\n/g, "\\n").replace(/\\n+$/,'')
                  });
                  parts.push(ph);
                  return;
                }

                const isBlock = [
                  'p', 'div', 'section', 'article', 'header', 'footer',
                  'ul', 'ol', 'li', 'blockquote',
                  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                  'table', 'thead', 'tbody', 'tr', 'td', 'th',
                  'hr', 'br'
                ].includes(tag);

                if (tag === 'br') {
                  parts.push('\\n');
                  return;
                }

                const children = Array.from(node.childNodes);

                if (isBlock && parts.length > 0) {
                  const last = parts[parts.length - 1] || '';
                  if (!last.endsWith('\\n')) parts.push('\\n');
                }

                for (const child of children) {
                  walk(child, parts, placeholders, codeBlocks);
                }

                if (isBlock) {
                  const last = parts[parts.length - 1] || '';
                  if (!last.endsWith('\\n')) parts.push('\\n');
                }
              }

              for (const node of nodes) {
                const role = node.getAttribute('data-message-author-role') || 'unknown';
                const parts = [];
                const placeholders = [];
                const codeBlocks = [];

                walk(node, parts, placeholders, codeBlocks);

                let textWithPlaceholders = parts.join('');
                textWithPlaceholders = textWithPlaceholders
                  .replace(/\\r\\n/g, "\\n")
                  .replace(/\\u00A0/g, ' ')
                  .replace(/[ \\t]+\\n/g, '\\n')
                  .replace(/\\n{3,}/g, '\\n\\n')
                  .trim();

                out.push({
                  role,
                  text_with_placeholders: textWithPlaceholders,
                  placeholders,
                  code_blocks: codeBlocks
                });
              }

              return out;
            }
            """,
            token_prefix
        )

    except Exception as e:
        ok = False
        reason = f"Exception: {type(e).__name__}: {e}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if ok:
        if status in (404, 410):
            ok = False
            reason = f"HTTP {status} (not found or removed)"
        elif status in (401, 403):
            ok = False
            reason = f"HTTP {status} (access denied)"
        elif payload is None:
            ok = False
            reason = "Page loaded but payload is empty"
        elif isinstance(payload, list) and len(payload) == 0:
            ok = False
            reason = "No message node found ([data-message-author-role])"

    messages: List[Dict[str, Any]] = []
    warnings: List[str] = []

    if ok and payload:
        for msg_idx, item in enumerate(payload):
            role = item.get("role", "unknown")
            text = item.get("text_with_placeholders", "") or ""
            placeholders = item.get("placeholders", []) or []
            blocks = item.get("code_blocks", []) or []

            if len(placeholders) != len(blocks):
                warnings.append(
                    f"url={url} msg_idx={msg_idx} mismatch placeholders={len(placeholders)} code_blocks={len(blocks)}"
                )

            list_of_code = []
            for i, block in enumerate(blocks):
                token = f"[[{token_prefix}_{msg_idx}_{i}]]"

                if i < len(placeholders):
                    text = text.replace(placeholders[i], token, 1)
                else:
                    text = (text + "\n" + token).strip()
                    warnings.append(
                        f"url={url} msg_idx={msg_idx} block_idx={i} placeholder missing; appended token at end"
                    )

                list_of_code.append({
                    "ReplaceString": token,
                    "Type": (block.get("language") or "").strip(),
                    "Content": _clean_text(block.get("code") or "")
                })

            messages.append({
                "role": role,
                "text": _clean_text(text),
                "ListOfCode": list_of_code
            })

    result = {
        "share_link": url,
        "ok": ok,
        "reason": reason,
        "http_status": status,
        "messages": messages
    }

    if debug:
        result["debug"] = {
            "diagnostics": diagnostics,
            "warnings": warnings,
        }
    elif warnings:
        # Keep warnings enabled even without debug mode to flag suspicious cases
        result["warnings"] = warnings

    return result


def read_share_urls_from_jsonl(path: str, field: str = "share_url") -> List[str]:
    urls: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = obj.get(field)
            if isinstance(u, str) and u.strip():
                urls.append(u.strip())
    return urls


def should_save(record: Dict[str, Any]) -> bool:
    return bool(record.get("ok")) and isinstance(record.get("messages"), list) and len(record["messages"]) > 0


def load_checkpoint(path: str) -> Dict[str, int]:
    p = Path(path)
    if not p.exists():
        return {"next_index": 0, "ok": 0, "fail": 0, "saved": 0, "skipped": 0}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"next_index": 0, "ok": 0, "fail": 0, "saved": 0, "skipped": 0}

    return {
        "next_index": int(data.get("next_index", 0)),
        "ok": int(data.get("ok", 0)),
        "fail": int(data.get("fail", 0)),
        "saved": int(data.get("saved", 0)),
        "skipped": int(data.get("skipped", 0)),
    }


def save_checkpoint(path: str, next_index: int, ok: int, fail: int, saved: int, skipped: int) -> None:
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    payload = {"next_index": next_index, "ok": ok, "fail": fail, "saved": saved, "skipped": skipped}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def load_already_saved_share_links(output_jsonl: str) -> Set[str]:
    saved: Set[str] = set()
    p = Path(output_jsonl)
    if not p.exists():
        return saved

    with open(output_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if should_save(obj):
                u = obj.get("share_link")
                if isinstance(u, str) and u.strip():
                    saved.add(u.strip())
    return saved


async def run(
    input_jsonl: str,
    output_jsonl: str,
    checkpoint_path: str,
    wait_ms: int,
    headless: bool,
    token_prefix: str,
    log_every: int,
    skip_if_already_saved: bool,
    debug: bool,
) -> Tuple[int, int, int, int, int]:
    urls = read_share_urls_from_jsonl(input_jsonl, field="share_url")
    total = len(urls)

    ckpt = load_checkpoint(checkpoint_path)
    start_index = min(max(ckpt["next_index"], 0), total)

    ok_count = ckpt["ok"]
    fail_count = ckpt["fail"]
    saved_count = ckpt["saved"]
    skipped_count = ckpt["skipped"]

    already_saved: Set[str] = set()
    if skip_if_already_saved:
        print(f"[DEDUP] Loading already-saved share_link from {output_jsonl} ...", file=sys.stderr)
        already_saved = load_already_saved_share_links(output_jsonl)
        print(f"[DEDUP] Found {len(already_saved)} already saved conversations.", file=sys.stderr)

    if start_index >= total:
        print(
            f"[CHECKPOINT] Already complete. total={total} ok={ok_count} fail={fail_count} "
            f"saved={saved_count} skipped={skipped_count}",
            file=sys.stderr
        )
        return total, ok_count, fail_count, saved_count, skipped_count

    print(f"[CHECKPOINT] Resuming from index {start_index}/{total}", file=sys.stderr)
    print(f"[OUTPUT] Appending to {output_jsonl}", file=sys.stderr)

    with open(output_jsonl, "a", encoding="utf-8") as out_f:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)

            for idx in tqdm(range(start_index, total), total=total, initial=start_index):
                url = urls[idx]

                if skip_if_already_saved and url in already_saved:
                    skipped_count += 1
                    next_index = idx + 1

                    if log_every > 0 and next_index % log_every == 0:
                        save_checkpoint(
                            checkpoint_path,
                            next_index,
                            ok_count,
                            fail_count,
                            saved_count,
                            skipped_count,
                        )
                        print(
                            f"[{next_index}/{total}] ok={ok_count} fail={fail_count} "
                            f"saved={saved_count} skipped={skipped_count}",
                            file=sys.stderr
                        )
                    continue

                rec = await reconstruct_conversation_with_inline_code_list_async(
                    browser=browser,
                    url=url,
                    wait_ms=wait_ms,
                    token_prefix=token_prefix,
                    debug=debug,
                )

                if rec.get("ok"):
                    ok_count += 1
                else:
                    fail_count += 1
                    print(f"[FAIL] url={url} reason={rec.get('reason')}", file=sys.stderr)

                if rec.get("warnings"):
                    for w in rec["warnings"]:
                        print(f"[WARN] {w}", file=sys.stderr)

                if debug and rec.get("debug", {}).get("warnings"):
                    for w in rec["debug"]["warnings"]:
                        print(f"[WARN][DEBUG] {w}", file=sys.stderr)

                if should_save(rec):
                    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out_f.flush()
                    saved_count += 1
                    if skip_if_already_saved:
                        already_saved.add(url)

                next_index = idx + 1
                if log_every > 0 and next_index % log_every == 0:
                    save_checkpoint(
                        checkpoint_path,
                        next_index,
                        ok_count,
                        fail_count,
                        saved_count,
                        skipped_count,
                    )
                    print(
                        f"[{next_index}/{total}] ok={ok_count} fail={fail_count} "
                        f"saved={saved_count} skipped={skipped_count}",
                        file=sys.stderr
                    )

            await browser.close()

    save_checkpoint(checkpoint_path, total, ok_count, fail_count, saved_count, skipped_count)
    print(
        f"[DONE] total={total} ok={ok_count} fail={fail_count} "
        f"saved={saved_count} skipped={skipped_count}",
        file=sys.stderr
    )
    return total, ok_count, fail_count, saved_count, skipped_count


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Scrape ChatGPT shared conversations and extract code blocks per message (checkpoint + dedup + DOM stabilization)."
    )
    ap.add_argument(
        "--input",
        default="github_file_share_links_1.jsonl",
        help="Input JSONL file (must contain share_url field)."
    )
    ap.add_argument(
        "--output",
        default="chatgpt_conversations_1.jsonl",
        help="Output JSONL file (append; only successful conversations)."
    )
    ap.add_argument(
        "--checkpoint",
        default="checkpoint_scrape_1.json",
        help="Checkpoint file path."
    )
    ap.add_argument(
        "--wait-ms",
        type=int,
        default=2500,
        help="Initial wait time after DOMContentLoaded (ms)."
    )
    ap.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless (default)."
    )
    ap.add_argument(
        "--headed",
        action="store_true",
        help="Run browser with UI (debug)."
    )
    ap.add_argument(
        "--token-prefix",
        default="CODE",
        help="Token prefix for code blocks, e.g. CODE -> [[CODE_3_0]]."
    )
    ap.add_argument(
        "--log-every",
        type=int,
        default=100,
        help="Log + checkpoint every N URLs."
    )
    ap.add_argument(
        "--skip-if-already-saved",
        action="store_true",
        help="Skip URLs already present in output JSONL (by share_link)."
    )
    ap.add_argument(
        "--debug",
        action="store_true",
        help="Include diagnostics and warnings in output records."
    )
    return ap


def main():
    ap = build_argparser()
    args = ap.parse_args()

    headless = True
    if args.headed:
        headless = False
    elif args.headless:
        headless = True

    asyncio.run(
        run(
            input_jsonl=args.input,
            output_jsonl=args.output,
            checkpoint_path=args.checkpoint,
            wait_ms=args.wait_ms,
            headless=headless,
            token_prefix=args.token_prefix,
            log_every=args.log_every,
            skip_if_already_saved=args.skip_if_already_saved,
            debug=args.debug,
        )
    )


if __name__ == "__main__":
    main()