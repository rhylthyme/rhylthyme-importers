"""
CookLang Importer - Convert .cook recipe files to Rhylthyme programs.

Uses cooklang-py for parsing. Install with:
    pip install "rhylthyme-importers[cooklang]"
"""

import os
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

import requests

from .base import BaseImporter, ImportResult, ImporterRegistry


# Cookware name keywords → Rhylthyme task type (checked in order; first match wins)
_COOKWARE_TASK_MAP = [
    ("oven",          ["oven", "broiler", "baking sheet", "roasting pan"]),
    ("stove-burner",  ["pan", "skillet", "wok", "pot", "saucepan", "frying pan", "dutch oven", "stovetop"]),
    ("grill",         ["grill", "barbecue", "bbq"]),
    ("microwave",     ["microwave"]),
    ("refrigeration", ["fridge", "refrigerator", "freezer"]),
]

_TASK_DEFAULTS: Dict[str, int] = {
    "stove-burner":  300,
    "oven":          1800,
    "prep-work":     180,
    "microwave":     120,
    "refrigeration": 1800,
    "grill":         600,
    "waiting":       600,
}

_CONSTRAINT_DEFAULTS = {
    "stove-burner":  (2, "Stove burners"),
    "oven":          (1, "Oven"),
    "prep-work":     (2, "Prep workspace"),
    "microwave":     (1, "Microwave"),
    "refrigeration": (2, "Refrigerator"),
    "grill":         (1, "Grill"),
    "waiting":       (4, "Passive waiting"),
}

# Verbs suggesting a step continues cooking on prior cookware (no new annotation needed).
_COOKING_CONTINUATION_VERBS = (
    "cook", "fry", "simmer", "boil", "sauté", "saute", "sear", "flip",
    "pour", "stir", "whisk", "toss", "reduce", "bake", "broil", "roast",
    "grill", "steam", "poach", "brown", "deglaze",
)

# Phrases that signal the recipe has moved away from the previous cookware.
_CONTEXT_SWITCH_KEYWORDS = (
    "meanwhile", "in another", "in a separate", "set aside",
    "transfer to", "remove from", "serve", "plate", "garnish",
    "let rest", "let cool",
)

# Narrower subset of the above: phrases that specifically signal a new,
# independent thread of work starting (not just "we've moved off the
# stovetop", which is still sequential — e.g. "remove from heat" or "let
# cool" continue the same dish, they just stop being active cooking).
# Only these justify skipping the default chain-to-previous-step fallback
# below and starting at programStart instead.
_PARALLEL_BRANCH_KEYWORDS = ("meanwhile", "in another", "in a separate")


def _looks_like_cooking_continuation(text: str, timings) -> bool:
    """True if a step with no explicit cookware still appears to be cooking
    on the cookware from the previous step."""
    text_l = text.lower()
    if any(kw in text_l for kw in _CONTEXT_SWITCH_KEYWORDS):
        return False
    if timings:
        return True
    if re.search(r"\d+\s*(?:to|-)?\s*\d*\s*(?:minute|min|hour|hr|second|sec)s?", text_l):
        return True
    return any(re.search(rf"\b{v}\b", text_l) for v in _COOKING_CONTINUATION_VERBS)


def github_blob_to_raw(url: str) -> str:
    """Convert a GitHub blob URL to a raw.githubusercontent.com URL.

    Non-GitHub URLs are returned unchanged.
    """
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)", url)
    if m:
        user, repo, rest = m.group(1), m.group(2), m.group(3)
        return f"https://raw.githubusercontent.com/{user}/{repo}/{rest}"
    return url


def _is_cook_input(s: str) -> bool:
    """True for .cook file paths, direct .cook URLs, and GitHub blob .cook URLs."""
    if s.endswith(".cook"):
        return True
    if re.match(r"https?://github\.com/.+/blob/.+\.cook$", s):
        return True
    return False


def _timer_to_seconds(timing) -> int:
    """Convert a cooklang-py Timing object to integer seconds."""
    try:
        amount = float(timing.quantity.amount)
    except (TypeError, ValueError, AttributeError):
        return 0
    unit = (getattr(timing.quantity, "unit", "") or "").lower().rstrip("s")
    unit_multipliers = {
        "second": 1, "sec": 1,
        "minute": 60, "min": 60,
        "hour": 3600, "hr": 3600,
    }
    return int(amount * unit_multipliers.get(unit, 60))


def _format_timing(timing) -> str:
    """Render a cooklang-py Timing object as human prose ('15 minutes').

    Anonymous timers (`~{15%minutes}`) have an empty `.name`, so falling back
    to `item.name` drops the duration entirely. Named timers (`~simmer{10%min}`)
    render as 'simmer (10 min)'.
    """
    q = getattr(timing, "quantity", None)
    amount = getattr(q, "amount", None) if q is not None else None
    unit = (getattr(q, "unit", "") or "").strip() if q is not None else ""
    name = (getattr(timing, "name", "") or "").strip()

    if amount is None or amount == "":
        return name

    if isinstance(amount, float) and amount.is_integer():
        amount_str = str(int(amount))
    else:
        amount_str = str(amount)
    time_str = f"{amount_str} {unit}".rstrip()
    return f"{name} ({time_str})" if name else time_str


_METADATA_LINE_RE = re.compile(r'>>[^>\n]*(?=>>|\n|$)')


def _strip_metadata_lines(text: str) -> str:
    """Remove cooklang inline-metadata segments (`>> key: value`) from prose.

    cooklang-py occasionally emits a phantom Step whose text is just the merged
    `>>` metadata lines (e.g. `>> source: Joy of Cooking >> serves: 12`).
    Stripping them here lets that phantom step collapse to empty so the
    importer's `if not text: continue` guard drops it.
    """
    if '>>' not in text:
        return text
    cleaned = _METADATA_LINE_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


def _step_to_text(step) -> str:
    """Reconstruct plain prose text from a cooklang-py Step."""
    parts = []
    for item in step:
        if isinstance(item, str):
            parts.append(item)
        elif type(item).__name__ == "Timing":
            parts.append(_format_timing(item))
        elif hasattr(item, "name"):
            parts.append(item.name)
    return _strip_metadata_lines("".join(parts).strip())


def _step_timings(step) -> list:
    """Return all Timing objects from a cooklang-py Step."""
    return [item for item in step if type(item).__name__ == "Timing"]


class CooklangImporter(BaseImporter):
    """Import CookLang .cook recipe files as Rhylthyme programs."""

    name = "cooklang"
    description = "Import CookLang (.cook) recipe files"
    supported_domains: List[str] = []

    def __init__(self, allow_local_files: bool = False):
        # Reading a local path is for the command line and for files this
        # process wrote itself (an upload). An importer reachable from user
        # input must never do it: whatever it reads comes back as recipe text.
        self.allow_local_files = allow_local_files

    def can_import(self, url_or_query: str) -> bool:
        return _is_cook_input(url_or_query)

    def search(self, query: str) -> List[Dict[str, Any]]:
        return []

    def import_from_url(self, url: str) -> ImportResult:
        """Import from a local path, direct URL, or GitHub blob URL."""
        try:
            fetch_url = github_blob_to_raw(url)
            if getattr(self, 'allow_local_files', False) and os.path.exists(url):
                content = Path(url).read_text(encoding="utf-8")
                source_name = Path(url).stem
            else:
                resp = requests.get(fetch_url, timeout=15)
                resp.raise_for_status()
                content = resp.text
                from urllib.parse import unquote
                source_name = unquote(Path(fetch_url.split("?")[0]).stem)
            return self.import_from_content(content, source_name=source_name, source_url=url)
        except Exception as e:
            return ImportResult(success=False, error=str(e))

    def import_from_content(
        self,
        content: str,
        source_name: str = "recipe",
        source_url: str = "",
    ) -> ImportResult:
        """Parse raw CookLang text and return an ImportResult."""
        try:
            from cooklang_py import Recipe
        except ImportError:
            return ImportResult(
                success=False,
                error='cooklang-py is required: pip install "rhylthyme-importers[cooklang]"',
            )

        try:
            recipe = Recipe(content)
        except Exception as e:
            return ImportResult(success=False, error=f"CookLang parse error: {e}")

        meta = recipe.metadata
        name = (
            meta.get("title")
            or source_name.replace("-", " ").replace("_", " ").title()
        )

        parsed_steps = []
        prev_cookware: list = []
        # Index (into parsed_steps) of the step that most recently
        # established the "current" cookware — either by naming it
        # explicitly or by continuing it. Lets _build_tracks resolve a
        # continuation step's real dependency once step ids are assigned.
        prev_cookware_step_idx: Optional[int] = None
        for i, step in enumerate(recipe.steps):
            text = _step_to_text(step)
            if not text:
                continue

            timings = _step_timings(step)

            # Carry cookware context forward: if this step has no explicit
            # cookware but the text reads as a cooking continuation, use the
            # cookware from the previous cooking step. Keeps steps like
            # "Pour in a ladle of batter and cook for 1–2 minutes" on the
            # stovetop track instead of falling back to prep-work.
            continuation_of_idx: Optional[int] = None
            if step.cookware:
                cookware = list(step.cookware)
                prev_cookware = cookware
                prev_cookware_step_idx = len(parsed_steps)
            elif prev_cookware and _looks_like_cooking_continuation(text, timings):
                cookware = prev_cookware
                continuation_of_idx = prev_cookware_step_idx
                prev_cookware_step_idx = len(parsed_steps)
            else:
                cookware = []

            task = self._task_from_cookware(cookware)
            duration = self._make_duration(timings, text, task)

            step_name = self.make_step_name(text)
            if step_name == "Prepare":
                # Fall back to named timer or generic ordinal
                named = next((t.name for t in timings if t.name), None)
                step_name = named.replace("-", " ").title() if named else f"Step {i + 1}"

            parsed_steps.append(
                {
                    "index": i,
                    "text": text,
                    "name": step_name,
                    "task": task,
                    "duration": duration,
                    "ingredients": step.ingredients,
                    "cookware_names": [cw.name.lower() for cw in cookware],
                    "_continuation_of_idx": continuation_of_idx,
                }
            )

        if not parsed_steps:
            return ImportResult(success=False, error="No steps found in recipe")

        program = self.create_base_program(
            name=name,
            description=meta.get("description") or f"Recipe: {name}",
            environment_type="kitchen",
            source_url=source_url,
            source_type="cooklang",
        )
        program["actors"] = 2

        # Frontmatter metadata
        if meta.get("servings"):
            program["metadata"]["servings"] = meta.get("servings")
        if meta.get("source"):
            program["metadata"]["source"]["url"] = meta.get("source")
        if meta.get("tags"):
            program["metadata"]["tags"] = meta.get("tags")

        program["metadata"]["ingredients"] = self._collect_ingredients(parsed_steps)
        program["tracks"] = self._build_tracks(parsed_steps)
        program["resourceConstraints"] = self._build_constraints(
            {s["task"] for s in parsed_steps}
        )

        return ImportResult(
            success=True,
            program=program,
            source_url=source_url,
            source_type="cooklang",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _task_from_cookware(self, cookware_list) -> str:
        """Map the first recognizable cookware item to a task type."""
        for cw in cookware_list:
            name_lower = cw.name.lower()
            for task, keywords in _COOKWARE_TASK_MAP:
                if any(kw in name_lower for kw in keywords):
                    return task
        return "prep-work"

    def _make_duration(self, timings: list, text: str, task: str) -> Dict[str, Any]:
        """Return a Rhylthyme duration dict from timer annotations or text hints."""
        # Explicit timer takes precedence
        if timings:
            secs = _timer_to_seconds(timings[0])
            if secs > 0:
                return {"type": "fixed", "seconds": secs}

        # Prose time range: "5 to 10 minutes" or "5-10 minutes"
        range_m = re.search(
            r"(\d+)\s*(?:to|-)\s*(\d+)\s*(minute|min|hour|hr|second|sec)s?",
            text,
            re.IGNORECASE,
        )
        if range_m:
            lo, hi = int(range_m.group(1)), int(range_m.group(2))
            mult = _unit_mult(range_m.group(3))
            return {
                "type": "variable",
                "minSeconds": lo * mult,
                "maxSeconds": hi * mult,
                "defaultSeconds": ((lo + hi) * mult) // 2,
            }

        # Single time mention in prose
        single_m = re.search(
            r"(\d+)\s*(?:further|more|additional|extra|other)?\s*(minute|min|hour|hr|second|sec)s?",
            text,
            re.IGNORECASE,
        )
        if single_m:
            return {
                "type": "fixed",
                "seconds": int(single_m.group(1)) * _unit_mult(single_m.group(2)),
            }

        # Keyword fallbacks
        text_l = text.lower()
        if any(w in text_l for w in ("quickly", "briefly")):
            return {"type": "fixed", "seconds": 60}
        if "overnight" in text_l:
            return {"type": "fixed", "seconds": 28800}

        # Short pan/grill "warm up" steps — melting butter, heating oil, etc.
        # Using the generic 5-minute stove-burner default leaves fat burning
        # while later ingredients are still being prepped.
        if task in ("stove-burner", "grill") and re.search(
            r"\b(melt|heat|warm|grease)\b", text_l
        ):
            return {"type": "fixed", "seconds": 60}

        return {"type": "fixed", "seconds": _TASK_DEFAULTS.get(task, 180)}

    def _build_tracks(self, parsed_steps: List[Dict]) -> List[Dict[str, Any]]:
        """Group steps into parallel tracks by task type (cookware-based inference)
        and compute each step's real startTrigger from two independent
        dependency signals, unioned together:

        1. Ingredient producer tracking — a data-flow graph over @-tagged
           ingredients. Each step "produces" every ingredient it references
           (it becomes the new producer, since it's now part of whatever
           combined mixture that step creates); a later step referencing
           an already-produced ingredient depends on its producer. Two or
           more distinct producers among a step's ingredients means a real
           merge point (independent prep threads converging).
        2. Cookware continuation — a step with no explicit cookware that
           reads as "still cooking on the same pan/oven" depends on
           whichever step last established that cookware. This exists
           because CookLang ingredient tags are purely lexical: "Pour in
           the batter" (untagged) or "Pour in the @batter" (a freshly
           invented name never `@`-produced under that name) both carry
           zero ingredient-producer signal on their own, so relying on
           ingredient tracking alone would silently drop this dependency.

        A step with candidates from neither signal is a genuine
        independent starting point (new cookware, new/untracked
        ingredients, no continuation cue) and starts at programStart —
        this is what lets truly parallel prep (e.g. creaming butter and
        sugar in one bowl while separately sifting dry ingredients in
        another) actually schedule in parallel instead of being forced
        into one artificial linear chain.
        """
        _TRACK_META = {
            "oven":          ("oven",          "Oven"),
            "stove-burner":  ("stovetop",       "Stovetop"),
            "grill":         ("grill",          "Grill"),
            "microwave":     ("microwave",      "Microwave"),
            "refrigeration": ("refrigeration",  "Refrigeration"),
            "waiting":       ("waiting",        "Waiting"),
            "prep-work":     ("prep",           "Preparation"),
        }

        # Pass 1 — assign a stepId to every step, numbered per track in recipe order.
        bucket_counters: Dict[str, int] = {}
        for s in parsed_steps:
            track_id, _ = _TRACK_META.get(s["task"], (s["task"], s["task"].title()))
            bucket_counters[track_id] = bucket_counters.get(track_id, 0) + 1
            s["_track_id"] = track_id
            s["_step_id"] = f"{track_id}_{bucket_counters[track_id]:02d}"

        idx_to_step_id = {i: s["_step_id"] for i, s in enumerate(parsed_steps)}

        # Pass 2 — assemble tracks, resolving each step's startTrigger from
        # the union of ingredient-producer and cookware-continuation signals.
        tracks_by_id: Dict[str, Dict[str, Any]] = {}
        producer_of: Dict[str, str] = {}  # ingredient name (lowercased) -> producing stepId
        # Every ingredient key a step "owns" — either tagged directly on it
        # or inherited from whatever it continues. Needed so a step that
        # continues cooking (e.g. "whisk in the eggs") without re-tagging
        # earlier ingredients still becomes their producer going forward;
        # otherwise a later step referencing one of those earlier tags by
        # name would resolve to the stale original step instead of the
        # continuation, silently skipping everything the continuation added.
        owned_by_step: Dict[str, set] = {}
        last_in_track: Dict[str, str] = {}
        branch_counts: Dict[str, int] = {}
        for i, s in enumerate(parsed_steps):
            step_id = s["_step_id"]
            track_id = s["_track_id"]
            _, track_name = _TRACK_META.get(s["task"], (s["task"], s["task"].title()))

            candidates: List[str] = []
            seen: set = set()

            def _add_candidate(sid: Optional[str]) -> None:
                if sid and sid != step_id and sid not in seen:
                    seen.add(sid)
                    candidates.append(sid)

            own_this_step: set = set()
            for ing in s["ingredients"]:
                key = ing.name.strip().lower()
                _add_candidate(producer_of.get(key))
                own_this_step.add(key)

            continuation_idx = s.get("_continuation_of_idx")
            if continuation_idx is not None:
                cont_step_id = idx_to_step_id.get(continuation_idx)
                _add_candidate(cont_step_id)
                own_this_step |= owned_by_step.get(cont_step_id, set())

            # Neither signal fired — this step neither continues an
            # ingredient's producer nor carries cookware forward (it names
            # its own cookware, or names none at all). Default to chaining
            # after the immediately preceding step, same as the recipe's
            # narrative order, UNLESS the step's own text explicitly signals
            # an independent parallel thread ("meanwhile", "in another
            # bowl", "in a separate pan") — that's the one positive signal
            # that overrides the sequential default and starts a real
            # parallel branch.
            if not candidates and i > 0:
                text_l = s["text"].lower()
                if not any(kw in text_l for kw in _PARALLEL_BRANCH_KEYWORDS):
                    _add_candidate(parsed_steps[i - 1]["_step_id"])

            # An independent step ("in a separate bowl") whose track is
            # already busy is a parallel branch: it gets its own track, so it
            # really runs alongside instead of overlapping.
            if not candidates and track_id in last_in_track:
                branch_counts[track_id] = branch_counts.get(track_id, 1) + 1
                n = branch_counts[track_id]
                track_id = f"{track_id}-{n}"
                track_name = f"{track_name} ({n})"
                s["_track_id"] = track_id

            # Steps in one track run one after another, whatever the
            # ingredient flow says, so the track's previous step is always a
            # dependency too. Without it a step that only depends on an
            # early step could be scheduled on top of the step before it in
            # the same track, and the program would not validate.
            _add_candidate(last_in_track.get(track_id))
            last_in_track[track_id] = step_id

            if not candidates:
                trigger: Dict[str, Any] = {"type": "programStart"}
            elif len(candidates) == 1:
                trigger = {"type": "afterStep", "stepId": candidates[0]}
            else:
                trigger = {
                    "logic": "all",
                    "triggers": [{"type": "afterStep", "stepId": c} for c in candidates],
                }

            # This step becomes the new producer for everything it owns —
            # its own tagged ingredients plus anything inherited above —
            # so downstream steps referencing any of them by name chain to
            # the most recent step that actually touched them.
            owned_by_step[step_id] = own_this_step
            for key in own_this_step:
                producer_of[key] = step_id

            track = tracks_by_id.setdefault(
                track_id,
                {"trackId": track_id, "name": track_name, "steps": []},
            )
            track["steps"].append(
                {
                    "stepId": step_id,
                    "name": s["name"],
                    "description": s["text"],
                    "task": s["task"],
                    "duration": s["duration"],
                    "startTrigger": trigger,
                }
            )

        return list(tracks_by_id.values())

    def _collect_ingredients(self, parsed_steps: List[Dict]) -> List[Dict[str, str]]:
        """Aggregate and deduplicate @ingredients across all steps."""
        seen: Dict[str, Dict[str, str]] = {}
        for s in parsed_steps:
            for ing in s["ingredients"]:
                key = ing.name.strip().lower()
                measure = ""
                if ing.quantity:
                    amt = str(ing.quantity.amount) if ing.quantity.amount is not None else ""
                    unit = str(ing.quantity.unit) if ing.quantity.unit else ""
                    measure = f"{amt} {unit}".strip()
                seen[key] = {"name": ing.name.strip(), "measure": measure}
        return list(seen.values())

    def _build_constraints(self, used_tasks: set) -> List[Dict[str, Any]]:
        """Emit resource constraints only for task types present in the recipe."""
        return [
            {"task": task, "maxConcurrent": max_c, "description": desc}
            for task, (max_c, desc) in _CONSTRAINT_DEFAULTS.items()
            if task in used_tasks
        ]


def _unit_mult(unit_str: str) -> int:
    unit = unit_str.lower()
    if unit.startswith("hour") or unit.startswith("hr"):
        return 3600
    if unit.startswith("sec"):
        return 1
    return 60  # minutes


ImporterRegistry.register(CooklangImporter())
