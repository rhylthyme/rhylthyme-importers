"""``BenchlingImporter`` — the BaseImporter wrapper for the three-module
Benchling pipeline.

Two entry points:
  - ``import_from_file(path, tenant)`` — read a saved Benchling API
    response, normalize, and build a program. No HTTP involved.
  - ``import_from_url(url, token)`` — detect tenant + resource type
    from a Benchling URL, fetch it live, then normalize + build.

Token resolution lives outside this module: the caller (Flask
upload endpoint or MCP tool handler) is responsible for fetching the
user's stored token from ``user_benchling`` and decrypting it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..base import BaseImporter, ImportResult
from .client import (
    BenchlingError,
    fetch_by_url,
    load_protocol_from_file,
    search_protocols,
)
from .normalizer import normalize_protocol
from .program_builder import build_program


def detect_tenant_from_url(url: str) -> Optional[str]:
    """Pull the tenant subdomain out of a Benchling URL.

    Returns ``None`` for non-Benchling URLs. Useful for the upload
    endpoint to look up which stored token to decrypt."""
    if not isinstance(url, str):
        return None
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return None
    if not host.endswith(".benchling.com"):
        return None
    return host.split(".benchling.com")[0] or None


class BenchlingImporter(BaseImporter):
    """Convert a Benchling Protocol / Workflow Task / Notebook Entry
    into a Rhylthyme program.
    """

    name = "benchling"
    description = "Import a Benchling Protocol into a Rhylthyme program"
    # URL pattern is `https://<tenant>.benchling.com/...protocols/<id>`.
    # Tenant-specific subdomains are matched at import time; this list
    # stays empty so the generic registry doesn't try to dispatch by
    # exact domain.
    supported_domains: List[str] = []

    def can_import(self, url_or_query: str) -> bool:
        if not isinstance(url_or_query, str):
            return False
        # Any Benchling URL OR a `.json` file (offline path).
        return ".benchling.com" in url_or_query or url_or_query.endswith(".json")

    def search(self, query: str) -> List[Dict[str, Any]]:
        # The base ``search`` API doesn't carry an auth token; live
        # search lives on ``search_live`` below. We return [] here so
        # the generic registry's discovery flow stays graceful when
        # the importer is registered but no user is connected.
        return []

    def search_live(
        self,
        *,
        tenant: str,
        token: str,
        query: str = "",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Live name-keyword search of the user's Benchling protocols.

        Caller supplies tenant + decrypted token; this module never
        looks them up. Returns the raw Benchling list shape (id, name,
        description) so the MCP tool can render its own markdown.
        """
        return search_protocols(tenant=tenant, token=token, query=query, limit=limit)

    def import_from_url(
        self,
        url: str,
        *,
        token: Optional[str] = None,
    ) -> ImportResult:
        """Live import via a Benchling URL. Requires a decrypted token
        supplied by the caller (the Flask endpoint that owns the
        ``user_benchling`` row)."""
        if not token:
            return ImportResult(
                success=False,
                error="Benchling URL import requires a connected account.",
                source_type="benchling",
            )
        tenant = detect_tenant_from_url(url)
        if not tenant:
            return ImportResult(
                success=False,
                error=f"Not a Benchling URL: {url}",
                source_type="benchling",
            )
        try:
            _shape, raw = fetch_by_url(url=url, token=token)
        except BenchlingError as e:
            return ImportResult(
                success=False,
                error=str(e),
                source_type="benchling",
            )
        return self._build_from_raw(raw, tenant=tenant)

    def import_from_file(self, path: str, *, tenant: str = "demo") -> ImportResult:
        """Convert a saved Benchling Protocol JSON into a Rhylthyme program.

        ``tenant`` is the Benchling subdomain — used only to construct
        the canonical source URL recorded in metadata. Defaults to
        ``demo`` so tests / examples can run without supplying one.
        """
        try:
            raw = load_protocol_from_file(path)
        except BenchlingError as e:
            return ImportResult(
                success=False,
                error=str(e),
                source_type="benchling",
            )
        return self._build_from_raw(raw, tenant=tenant)

    def import_from_raw(self, raw: Dict[str, Any], *, tenant: str = "demo") -> ImportResult:
        """Convert an already-loaded Benchling Protocol dict.

        Useful when the caller has the JSON in memory (e.g. from a
        Phase-3 HTTP fetch) and wants to share the normalize+build
        plumbing without going through the file path.
        """
        return self._build_from_raw(raw, tenant=tenant)

    def _build_from_raw(self, raw: Dict[str, Any], *, tenant: str) -> ImportResult:
        try:
            normalized = normalize_protocol(raw, tenant=tenant)
        except Exception as e:  # noqa: BLE001  — surfaces to caller as error
            return ImportResult(
                success=False,
                error=f"Benchling normalize failed: {e}",
                source_type="benchling",
            )
        try:
            program = build_program(normalized)
        except Exception as e:  # noqa: BLE001
            return ImportResult(
                success=False,
                error=f"Benchling program build failed: {e}",
                source_type="benchling",
            )
        return ImportResult(
            success=True,
            program=program,
            source_url=normalized.source_url,
            source_type="benchling",
        )
