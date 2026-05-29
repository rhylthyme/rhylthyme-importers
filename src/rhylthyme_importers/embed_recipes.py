"""Backfill ``programs.embedding`` with OpenAI embeddings.

Phase 2 of plans/search-quality.md. Idempotent and resumable: each run
picks up where the previous left off by selecting only rows where
``embedding IS NULL``.

Usage:
    rhylthyme-embed-recipes                       # all envs, all rows
    rhylthyme-embed-recipes --environment kitchen # one vertical
    rhylthyme-embed-recipes --limit 200           # cap this run
    rhylthyme-embed-recipes --dry-run             # print what would happen

Env vars required:
    OPENAI_API_KEY              - OpenAI key with embeddings scope
    SUPABASE_URL                - https://<project>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY   - service-role key (bypasses RLS so we
                                  can UPDATE the embedding column)

Cost: text-embedding-3-small is $0.02 per 1M tokens. A typical recipe
embed input (name + description) is 50–500 tokens. ~40K rows ≈ 4–20M
tokens ≈ $0.08–$0.40 for a full corpus backfill.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple


EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536


# ---------- Supabase REST helpers ---------------------------------------

def _supabase_request(
    method: str,
    path: str,
    *,
    body: Any = None,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Any]:
    """Thin wrapper for PostgREST calls using the service-role key."""
    url = f"{_must_env('SUPABASE_URL')}/rest/v1/{path}"
    key = _must_env("SUPABASE_SERVICE_ROLE_KEY")
    final_headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    if body is not None:
        final_headers["Content-Type"] = "application/json"
    if headers:
        final_headers.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=final_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8") or "null"
            return resp.status, json.loads(text)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"Supabase {method} {path} → {e.code}: {err_body}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Supabase unreachable: {e.reason}") from None


def _must_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} not set")
    return v.strip()


# ---------- OpenAI helpers ----------------------------------------------

def _openai_embed(texts: List[str]) -> Tuple[List[List[float]], Dict[str, int]]:
    """Call OpenAI's embeddings endpoint. Returns (vectors, usage)."""
    api_key = _must_env("OPENAI_API_KEY")
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"OpenAI embed failed ({e.code}): {err_body}") from None
    vectors = [item["embedding"] for item in payload.get("data", [])]
    usage = payload.get("usage", {})
    return vectors, usage


# ---------- Embedding source-text construction --------------------------

def _build_embed_text(name: Optional[str], description: Optional[str]) -> str:
    """Combine name + description into the string we feed the embedder.

    Truncate aggressively to keep token cost predictable — 4000 chars
    is well under the 8191-token limit of text-embedding-3-small and
    captures everything meaningful about a recipe.
    """
    n = (name or "").strip()
    d = (description or "").strip()
    if not n and not d:
        return ""
    joined = n + (" — " + d if d else "")
    return joined[:4000]


# ---------- Pipeline -----------------------------------------------------

def _fetch_pending(
    *, environment: Optional[str], limit: int, offset: int = 0,
) -> List[Dict[str, Any]]:
    """Pull rows that still need an embedding."""
    qs = ["embedding=is.null", "is_public=eq.true", "select=id,name,description"]
    if environment:
        qs.append(f"environment=eq.{environment}")
    qs.append(f"limit={limit}")
    if offset:
        qs.append(f"offset={offset}")
    qs.append("order=updated_at.desc")
    _, rows = _supabase_request("GET", f"programs?{'&'.join(qs)}")
    return rows or []


def _format_vector(vec: List[float]) -> str:
    """pgvector accepts a string literal like '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


def _write_embeddings(rows: List[Dict[str, Any]], vectors: List[List[float]]) -> int:
    """PATCH each row's embedding column. PostgREST batches via the
    array-payload form: ``PATCH ?id=in.(...)`` doesn't support
    per-row payloads, so we do one PATCH per row but they're tiny.
    """
    n = 0
    for row, vec in zip(rows, vectors):
        if len(vec) != EMBED_DIMS:
            raise RuntimeError(
                f"vector dim mismatch for {row['id']}: got {len(vec)}",
            )
        _supabase_request(
            "PATCH",
            f"programs?id=eq.{row['id']}",
            body={"embedding": _format_vector(vec)},
            headers={"Prefer": "return=minimal"},
        )
        n += 1
    return n


def run(
    *,
    environment: Optional[str] = None,
    batch_size: int = 100,
    limit: Optional[int] = None,
    dry_run: bool = False,
    sleep_s: float = 0.0,
) -> Dict[str, Any]:
    """Embed all pending rows in batches. Returns a summary dict."""
    started = time.time()
    total_rows = 0
    total_tokens = 0
    while True:
        remaining = (limit - total_rows) if limit is not None else batch_size
        if remaining <= 0:
            break
        fetch_n = min(batch_size, remaining)
        rows = _fetch_pending(environment=environment, limit=fetch_n)
        if not rows:
            break
        texts = [_build_embed_text(r.get("name"), r.get("description")) for r in rows]
        # Drop rows that would embed to empty.
        non_empty = [(r, t) for r, t in zip(rows, texts) if t.strip()]
        if not non_empty:
            # Mark them with a zero vector? No — leave NULL and skip
            # next time too. They'd just keep cycling through batches,
            # so we exit here. (In practice this only happens for
            # tests / hand-crafted rows with no name AND no desc.)
            print(
                f"warning: batch of {len(rows)} rows had no embedding "
                "text; stopping to avoid an infinite loop.",
                file=sys.stderr,
            )
            break
        rows_b = [r for r, _ in non_empty]
        texts_b = [t for _, t in non_empty]

        if dry_run:
            print(f"DRY-RUN: would embed {len(rows_b)} rows:")
            for r, t in zip(rows_b, texts_b):
                preview = t[:60].replace("\n", " ")
                print(f"  {r['id'][:8]}…  {preview!r}")
            total_rows += len(rows_b)
            if sleep_s:
                time.sleep(sleep_s)
            continue

        vectors, usage = _openai_embed(texts_b)
        if len(vectors) != len(rows_b):
            raise RuntimeError(
                f"OpenAI returned {len(vectors)} vectors for {len(rows_b)} inputs",
            )
        total_tokens += int(usage.get("total_tokens", 0))
        written = _write_embeddings(rows_b, vectors)
        total_rows += written
        cost = total_tokens / 1_000_000 * 0.02  # text-embedding-3-small
        print(
            f"embedded {total_rows} rows total · "
            f"{total_tokens} tokens · ${cost:.4f} so far",
        )
        if sleep_s:
            time.sleep(sleep_s)

    elapsed = time.time() - started
    cost = total_tokens / 1_000_000 * 0.02
    summary = {
        "rows_embedded": total_rows,
        "tokens": total_tokens,
        "cost_usd": cost,
        "elapsed_seconds": elapsed,
        "dry_run": dry_run,
    }
    print("done:", json.dumps(summary))
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--environment", help="restrict to one vertical (kitchen / lab / etc.)")
    p.add_argument("--batch-size", type=int, default=100,
                   help="rows per OpenAI call (default 100; max 2048 per OpenAI's limits)")
    p.add_argument("--limit", type=int,
                   help="cap total rows embedded this run (default: until pool empty)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would happen without calling OpenAI or writing")
    p.add_argument("--sleep", type=float, default=0.0,
                   help="seconds to sleep between batches (rate-limit safety)")
    args = p.parse_args()
    try:
        run(
            environment=args.environment,
            batch_size=args.batch_size,
            limit=args.limit,
            dry_run=args.dry_run,
            sleep_s=args.sleep,
        )
    except RuntimeError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
