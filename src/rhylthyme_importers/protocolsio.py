"""
Protocols.io Importer - Import laboratory protocols from protocols.io API.

API Documentation: https://apidoc.protocols.io/
"""

import requests
import re
import os
import html
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, parse_qs
from .base import BaseImporter, ImportResult, ImporterRegistry


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

    def _extract_duration(self, text: str, step_data: Dict) -> int:
        """Extract duration in seconds from step data or text."""
        # Check for explicit duration in API data
        if step_data.get("duration") and step_data["duration"] > 0:
            return step_data["duration"]

        # Try to parse from text
        patterns = [
            (r"(\d+)\s*(?:hours?|hrs?|h)", lambda m: int(m.group(1)) * 3600),
            (r"(\d+)\s*(?:minutes?|mins?|m)", lambda m: int(m.group(1)) * 60),
            (r"(\d+)\s*(?:seconds?|secs?|s)", lambda m: int(m.group(1))),
            (r"(\d+)\s*(?:days?|d)", lambda m: int(m.group(1)) * 86400),
            (r"(\d+):(\d+):(\d+)", lambda m: int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))),
            (r"(\d+):(\d+)", lambda m: int(m.group(1)) * 60 + int(m.group(2))),
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

    def _convert_to_program(self, protocol_data: Dict[str, Any], source_url: str) -> Dict[str, Any]:
        """Convert protocol data to Rhylthyme program."""
        # Extract metadata
        title = protocol_data.get("title", "Imported Protocol")
        description = self._clean_html(protocol_data.get("description", ""))

        # Extract authors
        authors = []
        creator = protocol_data.get("creator", {})
        if creator.get("name"):
            authors.append(creator["name"])
        for author in protocol_data.get("authors", []):
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

        # Extract and convert steps
        steps_data = protocol_data.get("steps", [])
        track_steps = []

        for i, step_data in enumerate(steps_data):
            # Skip substeps
            if step_data.get("is_substep"):
                continue

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
                "name": step_title if len(step_title) <= 45 else self.make_step_name(step_title),
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
