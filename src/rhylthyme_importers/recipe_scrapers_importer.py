"""
recipe-scrapers Importer — convert any URL supported by the open-source
``recipe-scrapers`` library into a Rhylthyme program.

``recipe-scrapers`` ships parsers for ~580 cooking sites. This importer is the
adapter that turns one scraper result into the same Rhylthyme program shape
TheMealDB and Cooklang produce, so the same execution / visualisation code
works.

For bulk mining + upload-to-Supabase see ``recipe_scrapers_mine.py``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .base import BaseImporter, ImportResult, ImporterRegistry


# Reuse the keyword tables from TheMealDB so per-site recipes get the same
# task assignments (stove-burner / oven / prep-work / …) as TheMealDB recipes.
from .themealdb import TheMealDBImporter as _TMDB


class RecipeScrapersImporter(BaseImporter):
    """Import any recipe-scrapers-supported recipe URL."""

    name = "recipe-scrapers"
    description = "Import recipes from ~580 cooking sites via the recipe-scrapers library"
    # supported_domains is populated lazily from the library since it has 500+ entries
    supported_domains: List[str] = []

    def __init__(self) -> None:
        # Defer the import so installs without recipe-scrapers don't break the
        # registry (the [project] dep covers normal installs but we keep this
        # tolerant).
        try:
            from recipe_scrapers import SCRAPERS  # type: ignore
            self._supported = set(SCRAPERS.keys())
        except Exception:  # pragma: no cover — recipe-scrapers should be a hard dep
            self._supported = set()
        self.supported_domains = sorted(self._supported)

    # ------------------------------------------------------------------ API

    def can_import(self, url_or_query: str) -> bool:
        host = self._host_of(url_or_query)
        if not host:
            return False
        # recipe-scrapers keys can include subdomains; match suffix.
        return any(host == d or host.endswith('.' + d) for d in self._supported)

    def search(self, query: str) -> List[Dict[str, Any]]:
        # recipe-scrapers has no search API; bulk mining uses a seed URL list.
        return []

    def import_from_url(self, url: str) -> ImportResult:
        try:
            from recipe_scrapers import scrape_me  # type: ignore
        except Exception as e:
            return ImportResult(success=False, error=f'recipe-scrapers not installed: {e}')

        try:
            scraper = scrape_me(url)
        except Exception as e:
            return ImportResult(success=False, error=f'scrape failed: {e}', source_url=url)

        try:
            program = self._convert(scraper, url)
        except Exception as e:
            return ImportResult(success=False, error=f'convert failed: {e}', source_url=url)

        return ImportResult(
            success=True, program=program, source_url=url, source_type='recipe-scrapers'
        )

    def import_from_html(self, html: str, url: str) -> ImportResult:
        """Parse already-fetched HTML — useful for retries / offline tests."""
        try:
            from recipe_scrapers import scrape_html  # type: ignore
        except Exception as e:
            return ImportResult(success=False, error=f'recipe-scrapers not installed: {e}')

        try:
            scraper = scrape_html(html=html, org_url=url)
        except Exception as e:
            return ImportResult(success=False, error=f'scrape_html failed: {e}', source_url=url)

        try:
            program = self._convert(scraper, url)
        except Exception as e:
            return ImportResult(success=False, error=f'convert failed: {e}', source_url=url)

        return ImportResult(
            success=True, program=program, source_url=url, source_type='recipe-scrapers'
        )

    # ----------------------------------------------------------- internals

    @staticmethod
    def _host_of(url: str) -> Optional[str]:
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            return None
        if host.startswith('www.'):
            host = host[4:]
        return host or None

    @staticmethod
    def _safe(callable_, default=None):
        """Call a recipe-scrapers method that may raise on missing data."""
        try:
            return callable_()
        except Exception:
            return default

    def _convert(self, scraper, url: str) -> Dict[str, Any]:
        title = (self._safe(scraper.title) or 'Untitled Recipe').strip()
        description = (self._safe(scraper.description) or '').strip()
        author = (self._safe(scraper.author) or '').strip()
        site_name = (self._safe(scraper.site_name) or '').strip()
        cuisine = (self._safe(scraper.cuisine) or '').strip()
        category = (self._safe(scraper.category) or '').strip()
        yields = (self._safe(scraper.yields) or '').strip()
        image = (self._safe(scraper.image) or '').strip()
        total_time = self._safe(scraper.total_time, 0) or 0  # minutes
        cook_time = self._safe(scraper.cook_time, 0) or 0
        prep_time = self._safe(scraper.prep_time, 0) or 0
        ingredients = self._safe(scraper.ingredients, []) or []
        instructions_list = self._safe(scraper.instructions_list, []) or []
        if not instructions_list:
            raw = self._safe(scraper.instructions, '') or ''
            instructions_list = [s.strip() for s in re.split(r'\n+|\r+', raw) if s.strip()]

        program = self.create_base_program(
            name=title,
            description=description or f'Recipe for {title}',
            environment_type='kitchen',
            source_url=url,
            source_type='recipe-scrapers',
        )
        program['actors'] = 2

        meta = program['metadata']
        if author:
            meta['author'] = author
        if site_name:
            meta['site_name'] = site_name
        if cuisine:
            meta['area'] = cuisine
        if category:
            meta['category'] = category
        if yields:
            meta['yields'] = yields
        if image:
            meta['thumbnail'] = image
        if total_time:
            meta['total_time_minutes'] = total_time
        if cook_time:
            meta['cook_time_minutes'] = cook_time
        if prep_time:
            meta['prep_time_minutes'] = prep_time
        # Persist raw ingredient strings (no measure-splitting; recipe-scrapers
        # returns them as a flat list of phrases like "1 tbsp olive oil").
        meta['ingredients'] = [{'name': ing, 'measure': ''} for ing in ingredients]

        # Cooking track — mirror TheMealDB's per-step structure so the existing
        # validators and visualisers Just Work.
        tmdb = _TMDB()
        track_steps: List[Dict[str, Any]] = []
        for i, step_text in enumerate(instructions_list):
            if len(step_text) < 5:
                continue
            duration_seconds = tmdb._estimate_duration(step_text)
            task = tmdb._determine_task(step_text)
            step = {
                'stepId': f'step_{i + 1:02d}',
                'name': self.make_step_name(step_text),
                'description': step_text,
                'task': task,
                'duration': {
                    'type': 'variable',
                    'minSeconds': max(60, duration_seconds // 2),
                    'maxSeconds': duration_seconds * 2,
                    'defaultSeconds': duration_seconds,
                },
            }
            if i == 0:
                step['startTrigger'] = {'type': 'programStart'}
            else:
                step['startTrigger'] = {'type': 'afterStep', 'stepId': f'step_{i:02d}'}
            track_steps.append(step)

        if not track_steps:
            track_steps.append({
                'stepId': 'step_01',
                'name': 'Prepare meal',
                'description': 'Follow recipe instructions',
                'task': 'prep-work',
                'duration': {'type': 'fixed', 'seconds': 1800},
                'startTrigger': {'type': 'programStart'},
            })

        prep_steps: List[Dict[str, Any]] = []
        if ingredients:
            prep_steps.append({
                'stepId': 'prep_ingredients',
                'name': 'Gather and prepare ingredients',
                'description': 'Gather: ' + ', '.join(ingredients),
                'task': 'prep-work',
                'duration': {'type': 'fixed', 'seconds': 300},
                'startTrigger': {'type': 'programStart'},
            })

        program['tracks'] = []
        if prep_steps:
            program['tracks'].append({
                'trackId': 'prep',
                'name': 'Preparation',
                'description': 'Ingredient preparation',
                'steps': prep_steps,
            })
        program['tracks'].append({
            'trackId': 'cooking',
            'name': 'Cooking Steps',
            'description': f'Main cooking steps for {title}',
            'steps': track_steps,
        })

        program['resourceConstraints'] = [
            {'task': 'stove-burner', 'maxConcurrent': 4, 'description': 'Stove burners'},
            {'task': 'oven', 'maxConcurrent': 1, 'description': 'Oven'},
            {'task': 'prep-work', 'maxConcurrent': 2, 'description': 'Prep workspace'},
            {'task': 'microwave', 'maxConcurrent': 1, 'description': 'Microwave'},
            {'task': 'refrigeration', 'maxConcurrent': 1, 'description': 'Refrigerator space'},
            {'task': 'waiting', 'maxConcurrent': 4, 'description': 'Passive waiting'},
        ]
        return program


ImporterRegistry.register(RecipeScrapersImporter())
