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
        """A kebab-case program id from a name: "Viral RNA Isolation (Magnetic
        Beads)" -> "viral-rna-isolation-magnetic-beads"."""
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:50].rstrip('-')
        return slug or 'imported-program'

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
            r'\s+for\s+\d+(?:\s*(?:to|-|\u2013|\u2014|further|more|additional|extra|other)\s*\d*)?\s*'
            r'(?:minute|min|hour|hr|second|sec)s?\b',
            '', cleaned, flags=re.IGNORECASE,
        )

        # Skip leading prepositional/adverbial phrases like
        # "In a 2-quart saucepan, heat the oil" → "Heat the oil"
        # ...and leading conditions: "When the potatoes are cool, peel them"
        # → "Peel them". The condition is in the description.
        prep_match = re.match(
            r'^(?:in|on|over|with|using|from|into|at|after|when|once|if|before|while|as soon as)\b[^,;.]*[,;]\s*',
            cleaned, flags=re.IGNORECASE,
        )
        if prep_match:
            cleaned = cleaned[prep_match.end():]

        # Try to grab the first verb phrase — up to the first period,
        # semicolon, "and", "until", "for about", or parenthetical.
        # Allow commas so we keep "Mix flour, sugar, and baking powder".
        # A subordinate clause ("so it's around 450°C", "because from now on
        # you wanna work fast", "by standing up some logs") explains the
        # step; the name is the step.
        # (A period inside a number, "3.5 litres", is not a sentence end.)
        m = re.match(
            r'([A-Za-z](?:[^;.()]|\.(?=\d))*?)'
            r'(?:\s+(?:until|for about|then|while|making sure|stirring|so that|so|because|which|by|if|unless)\b|[;()]|\.(?!\d))',
            cleaned,
        )
        name = m.group(1).strip() if m else cleaned
        # "Begin by frying the bacon" is not "Begin": only cut at "by" when
        # a real name is left.
        if m and re.search(r'\s+by\b', cleaned[:m.end()]) and len(name.split()) < 3:
            m2 = re.match(r'([A-Za-z](?:[^;.()]|\.(?=\d))*?)(?:\s+(?:until|then|while|so that|because|which)\b|[;()]|\.(?!\d))', cleaned)
            name = m2.group(1).strip() if m2 else cleaned

        # Cap the length. A whole clause is allowed to run a little long
        # (up to 52) rather than lose its last word ("...non-stick frying
        # pan"); otherwise cut at the last comma or "and" if that leaves a
        # real name, else at a word boundary. Never end on a word that needs
        # what came after it ("remove the pizza dough from the" ->
        # "remove the pizza dough").
        if len(name) > 52:
            head = name[:52]
            clause = max(head.rfind(','), head.rfind(' and '), head.rfind(' or '))
            if clause >= 18:
                name = head[:clause]
            else:
                truncated = head.rsplit(' ', 1)[0]
                # A cut that lands on a modifier ("into a large non-stick
                # frying", "to about") was mid-phrase: drop the whole trailing
                # prepositional phrase. A cut on a noun ("on the toasted
                # ciabatta") reads fine and stays.
                last = truncated.rsplit(' ', 1)[-1].lower()
                dangling = (last.endswith(('ing', 'ly')) or last in {
                    'a', 'an', 'the', 'about', 'rough', 'large', 'small', 'medium', 'big',
                    'little', 'hot', 'cold', 'warm', 'fresh', 'thin', 'thick', 'non-stick',
                    'each', 'every', 'some', 'few', 'several', 'more', 'less', 'very',
                })
                phrase = re.match(r'^(.{15,}\S)\s+(?:into|onto|in|on|to|with|over|for|from|at|of)\s+[^,]*$', truncated)
                if dangling and phrase:
                    truncated = phrase.group(1)
                name = truncated if len(truncated) > 10 else head
        name = re.sub(r',\s*\w+ing$', '', name)  # ", waiting" / ", stirring"
        # "into rough 2" (cm chunks) loses the number; "gas 4" keeps it.
        name = re.sub(r'\s+(?:rough|roughly|about|around|approximately|to|into|of)\s+\d+(?:\.\d+)?$', '', name)
        name = re.sub(
            r'(?:\s+(?:the|a|an|and|or|of|to|in|on|at|from|with|into|onto|over|for|your|some|that|this|it|its|is|are|as|but|not|if|unless|whether|where))+$',
            '', name.strip(), flags=re.IGNORECASE,
        ).rstrip(',:-')

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
