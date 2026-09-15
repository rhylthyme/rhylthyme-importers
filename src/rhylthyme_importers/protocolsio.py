"""
Protocols.io Importer - Import laboratory protocols from protocols.io API.

API Documentation: https://apidoc.protocols.io/
"""

import requests
import re
import os
import html
import random
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, parse_qs
from .base import BaseImporter, ImportResult, ImporterRegistry


def _truncate_step_title(title: str, limit: int = 45) -> str:
    """Word-boundary truncation for a "Step N: Section Title" label.
    Deliberately NOT BaseImporter.make_step_name() — that extracts a verb
    phrase from free-flowing instructional prose and stops at the first
    period, which mangles a numbered outline title like "Step 1: 1.
    Preparation of Allelic Entry-Clone Pools" down to "Step 1: 1" (it
    reads the list-numbering period as a sentence end)."""
    if len(title) <= limit:
        return title
    truncated = title[:limit].rsplit(" ", 1)[0]
    return (truncated if len(truncated) > 10 else title[:limit]) + "…"


def _protocol_step_sort_key(step_data: Dict[str, Any]):
    """Sort key for a protocols.io step by its "number" field (e.g. "5",
    "8.1"), not by its position in the API's steps array — see the call
    site for why the two can disagree. Unparseable/missing numbers sort
    last, after everything with a real number, rather than crashing."""
    try:
        return (0, float(step_data.get("number")))
    except (TypeError, ValueError):
        return (1, 0.0)


class ProtocolsIOImporter(BaseImporter):
    """Import protocols from protocols.io API."""

    name = "protocolsio"
    description = "Import laboratory protocols from protocols.io"
    supported_domains = ["protocols.io"]

    API_BASE = "https://www.protocols.io/api/v3"

    # Task type mappings based on keywords
    TASK_MAPPINGS = {
        "incubation": ["incubate", "incubation", "leave", "wait", "rest"],
        "centrifugation": ["centrifuge", "spin", "pellet"],
        "mixing": ["mix", "vortex", "shake", "stir", "agitate"],
        "pipetting": ["pipette", "add", "transfer", "dispense", "aliquot"],
        "heating": ["heat", "warm", "boil", "temperature"],
        "cooling": ["cool", "ice", "freeze", "refrigerate"],
        "washing": ["wash", "rinse", "clean"],
        "measurement": ["measure", "weigh", "volume", "read", "absorbance"],
        "observation": ["observe", "check", "examine", "inspect", "monitor"],
        "preparation": ["prepare", "set up", "arrange", "ready"]
    }

    # protocols.io's API has no native random/trending endpoint (confirmed
    # against the live API — /protocols requires a non-empty `key` search
    # term, unlike TheMealDB's dedicated random.php). get_random_protocol
    # simulates it: search a rotating common lab-technique term at a
    # random page offset, then pick a random result from that page.
    RANDOM_SEARCH_TERMS = [
        "extraction", "PCR", "cell culture", "assay", "western blot",
        "cloning", "sequencing", "purification", "immunostaining",
        "electrophoresis", "transfection", "chromatography", "microscopy",
        "ELISA", "staining", "genotyping", "flow cytometry", "protein",
        "centrifugation", "fixation",
    ]

    def __init__(self, access_token: Optional[str] = None):
        """
        Initialize with API access token.

        Args:
            access_token: protocols.io API token. If not provided, will try
                         PROTOCOLS_IO_TOKEN environment variable.
        """
        self.access_token = access_token or os.environ.get("PROTOCOLS_IO_TOKEN", "")
        self.session = requests.Session()
        if self.access_token:
            self.session.headers.update({
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json"
            })

    def can_import(self, url_or_query: str) -> bool:
        """Check if this importer can handle the input."""
        return "protocols.io" in url_or_query.lower()

    def search(self, query: str) -> List[Dict[str, Any]]:
        """Search for protocols by keyword."""
        if not self.access_token:
            return []

        try:
            response = self.session.get(
                f"{self.API_BASE}/protocols",
                params={"filter": "public", "key": query, "page_size": 20},
                timeout=15
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status_code") != 0:
                return []

            items = data.get("items", [])
            return [
                {
                    "name": item.get("title", "Untitled"),
                    "url": f"https://www.protocols.io/view/{item.get('uri', '')}",
                    "id": item.get("id"),
                    "description": self._clean_html(item.get("description", ""))[:200],
                    "author": item.get("creator", {}).get("name", "Unknown")
                }
                for item in items
            ]
        except Exception:
            return []

    def get_random_protocol(self) -> Optional[Dict[str, Any]]:
        """Get a random public protocol. protocols.io has no dedicated
        random endpoint, so this picks a random common lab-technique term
        and returns a random result from that search — same shape as
        `search()`'s items (has `id`, usable directly with
        `import_from_url`), matching the get_random_meal/get_random_recipe
        pattern the themealdb/spoonacular importers use."""
        term = random.choice(self.RANDOM_SEARCH_TERMS)
        results = self.search(term)
        return random.choice(results) if results else None

    def import_from_url(self, url: str) -> ImportResult:
        """Import a protocol from URL."""
        if not self.access_token:
            return ImportResult(
                success=False,
                error="protocols.io API token required. Set PROTOCOLS_IO_TOKEN environment variable."
            )

        try:
            # Parse URL to extract protocol ID
            url_info = self._parse_url(url)
            protocol_id = url_info.get("protocol_id")

            if not protocol_id:
                return ImportResult(
                    success=False,
                    error=f"Could not extract protocol ID from: {url}"
                )

            # Fetch protocol data
            protocol_data = self._fetch_protocol(protocol_id)
            if not protocol_data:
                return ImportResult(
                    success=False,
                    error=f"Protocol not found: {protocol_id}"
                )

            # Convert to Rhylthyme program
            program = self._convert_to_program(protocol_data, url)

            return ImportResult(
                success=True,
                program=program,
                source_url=url,
                source_type="protocolsio"
            )

        except Exception as e:
            return ImportResult(
                success=False,
                error=str(e)
            )

    def _parse_url(self, url: str) -> Dict[str, str]:
        """Parse protocols.io URL or bare protocol ID to extract protocol information."""
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")

        result = {"original_url": url}

        if len(path_parts) >= 2 and path_parts[0] == "view":
            result["protocol_id"] = path_parts[1]
            if len(path_parts) > 2:
                result["version"] = path_parts[2]
        elif not parsed.scheme and not parsed.netloc:
            # Bare protocol ID (e.g. "bc76izre")
            result["protocol_id"] = url.strip()

        # Check for step parameter
        query_params = parse_qs(parsed.query)
        if "step" in query_params:
            result["step_number"] = query_params["step"][0]

        return result

    def _fetch_protocol(self, protocol_id: str) -> Optional[Dict[str, Any]]:
        """Fetch protocol data from API."""
        try:
            response = self.session.get(
                f"{self.API_BASE}/protocols/{protocol_id}",
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status_code") != 0:
                return None

            return data.get("protocol", {})
        except Exception:
            return None

    def _clean_html(self, text) -> str:
        """Remove HTML tags from text."""
        if not text:
            return ""
        if isinstance(text, list):
            return " ".join(self._clean_html(item) for item in text if item)
        if not isinstance(text, str):
            return str(text)
        cleaned = re.sub(r"<[^>]+>", "", text).strip()
        return html.unescape(cleaned)

    def _extract_component_spans(self, raw_html: str, component_type: str) -> List[str]:
        """protocols.io's rich-text editor lets authors inline-tag pieces
        of a step's own text as structured "components" — reagents,
        durations, temperatures, centrifuge settings, etc. — rendered as
        `<span class="component-{type}">...</span>` within the step's raw
        HTML. This is a precise, author-intended signal completely
        discarded by _clean_html's blanket tag-stripping. Returns cleaned,
        non-empty span contents in document order (duplicates kept — a
        step can legitimately use the same reagent twice)."""
        if not raw_html:
            return []
        pattern = r'<span class="component-' + re.escape(component_type) + r'"[^>]*>(.*?)</span>'
        spans = re.findall(pattern, raw_html, re.IGNORECASE | re.DOTALL)
        return [c for c in (self._clean_html(s).strip() for s in spans) if c]

    def _parse_component_duration_seconds(self, raw_html: str) -> Optional[int]:
        """Sum every `component-duration` tag in a step's raw HTML
        (author-entered as HH:MM:SS). A step can have several distinct
        timed sub-actions ("spin 2 min, wash 5 min, spin 2 min") each
        tagged separately — summing gives the step's real total time.
        This is authoritative, not a guess, so when present it should be
        preferred over regex-guessing the duration from plain text."""
        total = 0
        matched_any = False
        for span in self._extract_component_spans(raw_html, "duration"):
            m = re.match(r"^(\d+):([0-5]\d):([0-5]\d)$", span.strip())
            if m:
                total += int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                matched_any = True
        return total if matched_any else None

    def _extract_duration(self, text: str, step_data: Dict) -> int:
        """Extract duration in seconds from step data or text."""
        # Highest priority: protocols.io's own inline component-duration
        # tags (see _parse_component_duration_seconds) — authoritative,
        # not a regex guess, so it sidesteps the mM/dilution-ratio false-
        # positive classes entirely for protocols that use it.
        component_seconds = self._parse_component_duration_seconds(step_data.get("step") or "")
        if component_seconds:
            return max(60, component_seconds)

        # Check for explicit duration in API data
        if step_data.get("duration") and step_data["duration"] > 0:
            return step_data["duration"]

        # Try to parse from text. Every unit pattern needs a trailing \b —
        # without it, "50mM Tris buffer" and "10mL of TBST" (extremely
        # common in wetlab protocols) get misread as "50 minutes" and
        # "10 minutes": the digits+letter match "50m"/"10m" happily
        # without a boundary check, silently producing wildly wrong
        # step durations across a large fraction of real imports.
        patterns = [
            (r"(\d+)\s*(?:hours?|hrs?|h)\b", lambda m: int(m.group(1)) * 3600),
            (r"(\d+)\s*(?:minutes?|mins?|m)\b", lambda m: int(m.group(1)) * 60),
            (r"(\d+)\s*(?:seconds?|secs?|s)\b", lambda m: int(m.group(1))),
            (r"(\d+)\s*(?:days?|d)\b", lambda m: int(m.group(1)) * 86400),
            # H:MM:SS / MM:SS — minutes/seconds groups constrained to a
            # valid 00-59 range with hard boundaries on both ends.
            # Without this, an antibody dilution ratio like "1:2000" or
            # "1:5000" (as common in western blot / IHC protocols as
            # concentration units are) gets read as a duration of
            # 1 min 2000 sec / 1 min 5000 sec.
            (r"\b(\d+):([0-5]\d):([0-5]\d)\b",
             lambda m: int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))),
            (r"\b(\d{1,2}):([0-5]\d)\b", lambda m: int(m.group(1)) * 60 + int(m.group(2))),
        ]

        for pattern, extractor in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    return max(60, extractor(match))
                except Exception:
                    continue

        return 300  # Default 5 minutes

    def _determine_task(self, text: str) -> str:
        """Determine task type from step text."""
        text_lower = text.lower()

        for task, keywords in self.TASK_MAPPINGS.items():
            if any(kw in text_lower for kw in keywords):
                return task

        return "lab-procedure"

    def _parse_materials(self, protocol_data: Dict[str, Any]) -> List[Dict[str, str]]:
        """Extract the protocol's materials/reagents list, trying
        progressively less-structured sources — each real protocol
        checked during development used a different one:
        1. A structured `materials` array (rare — the author filled in
           protocols.io's materials-table field).
        2. An HTML bullet list in `materials_text` (`<ul><li>...`).
        3. `materials_text` formatted as tagged reagent paragraphs
           instead of a bullet list (`<p><span class="component-
           reagent">...</span></p>` — protocols.io's own materials-list
           UI apparently generates this shape for some protocols).
        4. Aggregated `component-reagent` tags pulled directly out of
           individual step text. Many real protocols have NO
           materials_text at all (confirmed empirically: 16 of 25 random
           protocols sampled) but DO inline-tag reagents as the author
           writes each step, independent of maintaining a separate
           materials list. Without this tier, such protocols fall back
           to generic mode's single-row fold view instead of a real,
           multi-row one — this was the single biggest gap found."""
        materials: List[Dict[str, str]] = []
        seen = set()

        def _add(name: Optional[str]) -> None:
            name = (name or "").strip()
            if name and name not in seen:
                seen.add(name)
                materials.append({"name": name, "measure": ""})

        for item in protocol_data.get("materials") or []:
            if not isinstance(item, dict):
                continue
            raw_name = item.get("name") or item.get("title") or item.get("text")
            _add(self._clean_html(raw_name) if raw_name else None)

        materials_text = protocol_data.get("materials_text") or ""

        if not materials:
            for li in re.findall(r"<li[^>]*>(.*?)</li>", materials_text, re.IGNORECASE | re.DOTALL):
                _add(self._clean_html(li))

        if not materials:
            for name in self._extract_component_spans(materials_text, "reagent"):
                _add(name)

        if not materials:
            # Some protocols format materials_text as one paragraph per
            # item — with a leading "- " bullet marker (id 318778:
            # "<p>- HSM</p><p>- CAR T PE (BD/624255)</p>...") or without
            # one at all (id 107710: "<p>AMPure XP beads</p><p>DNA LoBind
            # Tubes...</p>") — rather than an <li> list or tagged spans.
            # Two things get filtered out, neither a real material name:
            # a bare header line ("Reagent/Supplies:") and an embedded
            # table's own caption (id 321704 formats materials_text as
            # actual <table> markup with captions like "Table 1:
            # Specifications of the equipment" — parsing real reagent
            # names back out of arbitrary table cells isn't attempted;
            # better to surface nothing than a caption masquerading as
            # an ingredient row).
            for p in re.findall(r"<p[^>]*>(.*?)</p>", materials_text, re.IGNORECASE | re.DOTALL):
                cleaned = self._clean_html(p).strip()
                cleaned = re.sub(r"^[-•*]\s*", "", cleaned)
                if cleaned and not cleaned.endswith(":") and not re.match(r"^Table\s+\d+\s*:", cleaned, re.IGNORECASE):
                    _add(cleaned)

        if not materials:
            for step in protocol_data.get("steps") or []:
                for name in self._extract_component_spans(step.get("step") or "", "reagent"):
                    _add(name)

        return materials

    def _convert_to_program(self, protocol_data: Dict[str, Any], source_url: str) -> Dict[str, Any]:
        """Convert protocol data to Rhylthyme program."""
        # Extract metadata
        title = self._clean_html(protocol_data.get("title") or "Imported Protocol")
        description = self._clean_html(protocol_data.get("description", ""))

        # Extract authors. `or {}`/`or []`, not a `.get(key, default)`
        # default — protocols.io returns these keys present but explicitly
        # null for some protocols (e.g. a protocol whose real content is
        # an uploaded .docx with no structured steps at all), and `.get`'s
        # default only kicks in when the key is missing, not when its
        # value is None. The `steps` case below hit this for real and
        # crashed the whole import instead of reaching the existing
        # "no steps, use a placeholder" fallback.
        authors = []
        creator = protocol_data.get("creator") or {}
        if creator.get("name"):
            authors.append(creator["name"])
        for author in protocol_data.get("authors") or []:
            if isinstance(author, dict) and author.get("name"):
                if author["name"] not in authors:
                    authors.append(author["name"])

        # Create base program
        program = self.create_base_program(
            name=title,
            description=description or f"Protocol imported from protocols.io",
            environment_type="laboratory",
            source_url=source_url,
            source_type="protocolsio"
        )

        # One operator by default
        program["actors"] = 1

        # Add additional metadata
        if authors:
            program["metadata"]["authors"] = authors
        if protocol_data.get("doi"):
            program["metadata"]["doi"] = protocol_data["doi"]
        if protocol_data.get("uri"):
            program["metadata"]["uri"] = protocol_data["uri"]

        # Materials/reagents, analogous to a recipe's ingredients — feeds
        # metadata.ingredients so the fold view's recipe mode (rows =
        # materials, brackets = steps that use them) works for lab
        # protocols exactly as it does for recipes, with no separate
        # lab-specific code path needed.
        materials = self._parse_materials(protocol_data)
        if materials:
            program["metadata"]["ingredients"] = materials

        # Extract and convert steps. protocols.io's `steps` array is NOT
        # guaranteed to be in execution order — a real protocol was found
        # with array order 5, 9, 8, 7, 6, 1, 4, 3, 2, 10 (each item's own
        # "number" field is the true intended sequence; array position
        # apparently reflects something else, like edit history). Sorting
        # by number before building the sequential afterStep chain is
        # what makes the resulting program's step order match the
        # protocol's actual numbered outline instead of scrambling it.
        steps_data = sorted(
            (s for s in (protocol_data.get("steps") or []) if not s.get("is_substep")),
            key=_protocol_step_sort_key,
        )
        track_steps = []

        for i, step_data in enumerate(steps_data):
            step_num = step_data.get("number", str(i + 1))
            section = self._clean_html(step_data.get("section", ""))
            step_content = self._clean_html(step_data.get("step", ""))

            # Build step title
            if section:
                step_title = f"Step {step_num}: {section}"
            else:
                step_title = f"Step {step_num}"

            # Get full text for analysis
            full_text = f"{step_title} {step_content}"

            # Extract duration and task
            duration = self._extract_duration(full_text, step_data)
            task = self._determine_task(full_text)

            step_id = f"step_{i+1:02d}"

            step_entry = {
                "stepId": step_id,
                "name": _truncate_step_title(step_title),
                "description": step_content or step_title,
                "task": task,
                "duration": {
                    "type": "variable",
                    "minSeconds": max(60, duration // 2),
                    "maxSeconds": duration * 2,
                    "defaultSeconds": duration
                }
            }

            # Add start trigger
            if i == 0:
                step_entry["startTrigger"] = {"type": "programStart"}
            else:
                step_entry["startTrigger"] = {
                    "type": "afterStep",
                    "stepId": f"step_{i:02d}"
                }

            # Extract critical information as notes
            critical = self._clean_html(step_data.get("critical", ""))
            if critical:
                step_entry["notes"] = f"Critical: {critical}"

            # This step's own inline-tagged reagent mentions (see
            # _extract_component_spans) — an authoritative record of
            # which materials THIS step actually uses, straight from the
            # protocol author's own markup. fold_view.py's recipe mode
            # reads step.metadata.mentionedIngredients when present and
            # matches it directly against metadata.ingredients instead of
            # fuzzy-guessing from step text, the same override pattern
            # already used for metadata.fold / metadata.foldLabel.
            mentioned = self._extract_component_spans(step_data.get("step") or "", "reagent")
            if mentioned:
                step_entry["metadata"] = {"mentionedIngredients": mentioned}

            track_steps.append(step_entry)

        # If no steps, create a placeholder
        if not track_steps:
            track_steps.append({
                "stepId": "step_01",
                "name": "Execute protocol",
                "description": description or "Follow protocol instructions",
                "task": "lab-procedure",
                "duration": {"type": "fixed", "seconds": 1800},
                "startTrigger": {"type": "programStart"}
            })

        # Build main track
        program["tracks"] = [{
            "trackId": "main-protocol",
            "name": "Main Protocol Steps",
            "description": f"Main execution track for {title}",
            "steps": track_steps
        }]

        # Set resource constraints for laboratory
        program["resourceConstraints"] = [
            {"task": "lab-procedure", "maxConcurrent": 4, "description": "General lab procedures"},
            {"task": "pipetting", "maxConcurrent": 2, "description": "Pipetting operations"},
            {"task": "centrifugation", "maxConcurrent": 1, "description": "Centrifuge usage"},
            {"task": "incubation", "maxConcurrent": 3, "description": "Incubation steps"},
            {"task": "heating", "maxConcurrent": 2, "description": "Heating equipment"},
            {"task": "cooling", "maxConcurrent": 2, "description": "Cooling operations"},
            {"task": "mixing", "maxConcurrent": 2, "description": "Mixing operations"},
            {"task": "washing", "maxConcurrent": 2, "description": "Washing operations"},
            {"task": "measurement", "maxConcurrent": 1, "description": "Measurement equipment"},
            {"task": "observation", "maxConcurrent": 4, "description": "Visual observation"},
            {"task": "preparation", "maxConcurrent": 3, "description": "Sample preparation"}
        ]

        return program


# Register the importer
ImporterRegistry.register(ProtocolsIOImporter())
