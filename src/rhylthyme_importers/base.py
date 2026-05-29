"""
Base importer class and registry for Rhylthyme importers.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime
import re


@dataclass
class ImportResult:
    """Result of an import operation."""
    success: bool
    program: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    source_url: Optional[str] = None
    source_type: str = "unknown"


class BaseImporter(ABC):
    """Base class for all Rhylthyme importers."""

    # Subclasses should override these
    name: str = "base"
    description: str = "Base importer"
    supported_domains: List[str] = []

    @abstractmethod
    def can_import(self, url_or_query: str) -> bool:
        """Check if this importer can handle the given URL or query."""
        pass

    @abstractmethod
    def import_from_url(self, url: str) -> ImportResult:
        """Import a program from a URL."""
        pass

    @abstractmethod
    def search(self, query: str) -> List[Dict[str, Any]]:
        """Search for importable items. Returns list of {name, url, description}."""
        pass

    def generate_program_id(self, name: str) -> str:
        """Generate a safe program ID from a name."""
        return re.sub(r'[^a-zA-Z0-9_-]', '_', name.lower())[:50]

    @staticmethod
    def make_step_name(text: str) -> str:
        """Create a concise, action-oriented step name from instruction text.

        Extracts the core verb phrase (e.g. "Sauté onions", "Simmer 20 min")
        instead of blindly truncating the full instruction.
        """
        # Strip leading connectors / filler
        cleaned = re.sub(
            r'^(then|next|now|after that|once done|when ready|finally|afterwards)[,\s]+',
            '', text.strip(), flags=re.IGNORECASE,
        )

        # Drop "for 15 minutes" / "for 1 to 2 hours" style duration phrases — the
        # step's duration field already surfaces them in the UI, and keeping them
        # here often forces an ugly mid-number truncation at 45 chars.
        cleaned = re.sub(
            r'\s+for\s+\d+(?:\s*(?:to|-|further|more|additional|extra|other)\s*\d*)?\s*'
            r'(?:minute|min|hour|hr|second|sec)s?\b',
            '', cleaned, flags=re.IGNORECASE,
        )

        # Skip leading prepositional/adverbial phrases like
        # "In a 2-quart saucepan, heat the oil" → "Heat the oil"
        prep_match = re.match(
            r'^(?:in|on|over|with|using|from|into|at|after)\b[^,;.]*[,;]\s*',
            cleaned, flags=re.IGNORECASE,
        )
        if prep_match:
            cleaned = cleaned[prep_match.end():]

        # Try to grab the first verb phrase — up to the first period,
        # semicolon, "and", "until", "for about", or parenthetical.
        # Allow commas so we keep "Mix flour, sugar, and baking powder".
        m = re.match(
            r'([A-Za-z][^;.()]*?)'
            r'(?:\s+(?:until|for about|then|while|making sure|stirring)\b|[;.()])',
            cleaned,
        )
        name = m.group(1).strip() if m else cleaned

        # Cap at 45 chars on a word boundary
        if len(name) > 45:
            truncated = name[:45].rsplit(' ', 1)[0]
            name = truncated if len(truncated) > 10 else name[:45]

        # Capitalise first letter
        if name:
            name = name[0].upper() + name[1:]

        return name or "Prepare"

    def create_base_program(
        self,
        name: str,
        description: str,
        environment_type: str,
        source_url: str,
        source_type: str
    ) -> Dict[str, Any]:
        """Create a base program structure."""
        return {
            "programId": self.generate_program_id(name),
            "name": name,
            "description": description,
            "version": "1.0.0",
            "environmentType": environment_type,
            "startTrigger": {"type": "manual"},
            "tracks": [],
            "resourceConstraints": [],
            "metadata": {
                "source": {
                    "type": source_type,
                    "url": source_url,
                    "imported_at": datetime.now().isoformat(),
                    "importer": self.name
                }
            }
        }


class ImporterRegistry:
    """Registry for available importers."""

    _importers: Dict[str, BaseImporter] = {}

    @classmethod
    def register(cls, importer: BaseImporter) -> None:
        """Register an importer."""
        cls._importers[importer.name] = importer

    @classmethod
    def get(cls, name: str) -> Optional[BaseImporter]:
        """Get an importer by name."""
        return cls._importers.get(name)

    @classmethod
    def get_all(cls) -> Dict[str, BaseImporter]:
        """Get all registered importers."""
        return cls._importers.copy()

    @classmethod
    def find_for_url(cls, url: str) -> Optional[BaseImporter]:
        """Find an importer that can handle the given URL."""
        for importer in cls._importers.values():
            if importer.can_import(url):
                return importer
        return None

    @classmethod
    def list_importers(cls) -> List[Dict[str, Any]]:
        """List all available importers with their info."""
        return [
            {
                "name": imp.name,
                "description": imp.description,
                "supported_domains": imp.supported_domains
            }
            for imp in cls._importers.values()
        ]
