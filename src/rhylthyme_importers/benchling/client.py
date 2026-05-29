"""HTTP client for Benchling's REST API.

Phase 4: live ``GET`` against the user's tenant for Protocol / Workflow
Task / Notebook Entry resources, plus a name-keyword search across
Protocols. Phase 1's offline helper (``load_protocol_from_file``)
stays around for tests and the CLI's ``--from-file`` mode.

Auth: the caller supplies the API token (decrypted upstream by the
Flask endpoint that owns the ``user_benchling`` row). Tokens are
NEVER persisted or logged from inside this module.

Errors: every HTTP failure surfaces as ``BenchlingError`` with a
human-friendly message; the raw response body is included only for
non-2xx statuses, so a missing-protocol 404 says "protocol not found"
rather than dumping JSON to the user.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_TIMEOUT_SECONDS = 12


class BenchlingError(Exception):
    """Wraps any Benchling-side failure (HTTP error, malformed payload).

    Carries the HTTP status when available so callers can decide whether
    to retry (5xx, 429) vs. surface to the user (4xx).
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def load_protocol_from_file(path: str) -> Dict[str, Any]:
    """Read a saved Benchling Protocol API response from a JSON file.

    Used by tests and by the CLI's ``--from-file`` mode. Lets the rest
    of the pipeline run end-to-end with no network dependency.
    """
    p = Path(path)
    if not p.exists():
        raise BenchlingError(f"no such file: {path}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BenchlingError(f"invalid JSON at {path}: {e}") from e


# ---- Live HTTP -----------------------------------------------------------

def _request_json(
    method: str,
    url: str,
    *,
    token: str,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Send a JSON request to Benchling and return the parsed response.

    Raises BenchlingError on non-2xx or transport failure. Keeps the
    token in the Authorization header only — never logged.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    data: Optional[bytes] = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            text = resp.read().decode("utf-8") or "null"
            return json.loads(text)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            err_body = ""
        if e.code == 401:
            raise BenchlingError(
                "Benchling rejected the token. Reconnect in settings.",
                status=401,
            ) from None
        if e.code == 404:
            raise BenchlingError("Benchling resource not found.", status=404) from None
        if e.code == 429:
            raise BenchlingError(
                "Benchling rate-limited the request. Try again shortly.",
                status=429,
            ) from None
        raise BenchlingError(
            f"Benchling HTTP {e.code}: {err_body or 'no details'}",
            status=e.code,
        ) from None
    except urllib.error.URLError as e:
        raise BenchlingError(f"Benchling unreachable: {e.reason}") from None
    except json.JSONDecodeError as e:
        raise BenchlingError(f"Benchling returned non-JSON: {e}") from None


def fetch_protocol(*, tenant: str, token: str, protocol_id: str) -> Dict[str, Any]:
    """``GET /api/v2/protocols/{id}`` on the user's tenant."""
    base = f"https://{tenant}.benchling.com/api/v2/protocols/{urllib.parse.quote(protocol_id, safe='')}"
    return _request_json("GET", base, token=token)


def fetch_workflow_task(*, tenant: str, token: str, task_id: str) -> Dict[str, Any]:
    """``GET /api/v2/workflow-tasks/{id}``."""
    base = f"https://{tenant}.benchling.com/api/v2/workflow-tasks/{urllib.parse.quote(task_id, safe='')}"
    return _request_json("GET", base, token=token)


def fetch_entry(*, tenant: str, token: str, entry_id: str) -> Dict[str, Any]:
    """``GET /api/v2/entries/{id}`` (Notebook Entry)."""
    base = f"https://{tenant}.benchling.com/api/v2/entries/{urllib.parse.quote(entry_id, safe='')}"
    return _request_json("GET", base, token=token)


def fetch_by_url(*, token: str, url: str) -> Tuple[str, Dict[str, Any]]:
    """Detect which Benchling resource a URL points at and fetch it.

    Returns ``(shape, raw)`` where ``shape`` is one of ``"protocol"``,
    ``"workflow_task"``, ``"entry"`` so the normalizer can route. Raises
    BenchlingError on unrecognized URLs.

    URL examples:
      https://acme.benchling.com/acme/f/lib_xxx-protocols/protocols/prot_abc/edit
      https://acme.benchling.com/.../workflow-tasks/wftask_xxx
      https://acme.benchling.com/.../entries/etr_xxx
    """
    parsed = urllib.parse.urlparse(url)
    host = (parsed.netloc or "").lower()
    if not host.endswith(".benchling.com"):
        raise BenchlingError(f"not a Benchling URL: {url}")
    tenant = host.split(".benchling.com")[0]
    path = parsed.path or ""

    # Path patterns include the resource type as a segment followed by
    # the id. Walk the path segments to find a known pair.
    parts = [p for p in path.split("/") if p]
    KNOWN = {
        "protocols": ("protocol", fetch_protocol, "protocol_id"),
        "workflow-tasks": ("workflow_task", fetch_workflow_task, "task_id"),
        "entries": ("entry", fetch_entry, "entry_id"),
    }
    for i, seg in enumerate(parts[:-1]):
        if seg in KNOWN:
            shape, fetcher, arg_name = KNOWN[seg]
            rid = parts[i + 1]
            kw = {arg_name: rid, "tenant": tenant, "token": token}
            return shape, fetcher(**kw)
    raise BenchlingError(
        f"Couldn't detect a Benchling resource type in URL: {url}",
    )


def search_protocols(
    *,
    tenant: str,
    token: str,
    query: str = "",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Name-keyword search across the user's Benchling Protocols.

    Returns a list of summary objects ``{id, name, description?}``.
    Uses ``GET /api/v2/protocols`` with the ``nameIncludes`` query
    parameter (Benchling's pattern).
    """
    params = {"pageSize": str(min(max(1, limit), 50))}
    if query:
        params["nameIncludes"] = query
    qs = urllib.parse.urlencode(params)
    url = f"https://{tenant}.benchling.com/api/v2/protocols?{qs}"
    data = _request_json("GET", url, token=token)
    rows = data.get("protocols") or data.get("results") or []
    return [r for r in rows if isinstance(r, dict)]


def validate_token(*, tenant: str, token: str) -> Optional[Dict[str, Any]]:
    """``GET /api/v2/users/me`` — returns the parsed body on 200, or
    None on any auth/transport failure. Used by the Flask connect
    endpoint to refuse storing a bad token."""
    url = f"https://{tenant}.benchling.com/api/v2/users/me"
    try:
        return _request_json("GET", url, token=token)
    except BenchlingError:
        return None
