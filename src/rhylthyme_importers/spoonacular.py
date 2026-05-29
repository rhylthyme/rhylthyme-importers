"""
Spoonacular Importer - Import recipes from the Spoonacular API as Rhylthyme cooking programs.

API Documentation: https://spoonacular.com/food-api/docs
"""

import requests
import re
import os
from typing import Dict, Any, List, Optional
from .base import BaseImporter, ImportResult, ImporterRegistry


class SpoonacularImporter(BaseImporter):
    """Import recipes from the Spoonacular API."""

    name = "spoonacular"
    description = "Import recipes from Spoonacular (rich recipe API with nutrition and equipment data)"
    supported_domains = ["spoonacular.com"]

    API_BASE = "https://api.spoonacular.com"

    # Map Spoonacular equipment to cooking task types
    EQUIPMENT_TASK_MAP = {
        "oven": "oven",
        "toaster oven": "oven",
        "broiler": "oven",
        "stove": "stove-burner",
        "burner": "stove-burner",
        "frying pan": "stove-burner",
        "skillet": "stove-burner",
        "saucepan": "stove-burner",
        "pot": "stove-burner",
        "wok": "stove-burner",
        "dutch oven": "stove-burner",
        "grill": "grill",
        "microwave": "microwave",
        "blender": "prep-work",
        "food processor": "prep-work",
        "mixer": "prep-work",
        "cutting board": "prep-work",
        "bowl": "prep-work",
        "whisk": "prep-work",
        "knife": "prep-work",
        "refrigerator": "refrigeration",
        "freezer": "refrigeration",
    }

    # Keyword-based task detection (fallback when equipment doesn't map)
    TASK_KEYWORDS = {
        "stove-burner": [
            "fry", "sauté", "saute", "simmer", "boil", "cook on",
            "pan", "skillet", "wok", "saucepan", "reduce", "sear",
        ],
        "oven": [
            "bake", "roast", "broil", "oven", "toast", "preheat",
        ],
        "grill": [
            "grill", "barbecue", "bbq", "char",
        ],
        "prep-work": [
            "chop", "dice", "slice", "mince", "cut", "peel", "grate",
            "mix", "combine", "stir", "whisk", "beat", "fold", "season",
            "marinate", "coat", "dredge", "prepare", "arrange", "toss",
        ],
        "microwave": [
            "microwave",
        ],
        "refrigeration": [
            "refrigerate", "chill", "cool", "rest in fridge", "freeze",
        ],
        "waiting": [
            "rest", "stand", "wait", "let sit", "rise", "proof", "set aside",
        ],
    }

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize with API key.

        Args:
            api_key: Spoonacular API key. If not provided, checks
                     SPOONACULAR_API_KEY env var, then falls back to default.
        """
        self.api_key = api_key or os.environ.get("SPOONACULAR_API_KEY", "")
        self.session = requests.Session()

    def _params(self, **kwargs) -> Dict[str, Any]:
        """Build request params with API key included."""
        kwargs["apiKey"] = self.api_key
        return kwargs

    def can_import(self, url_or_query: str) -> bool:
        """Check if this importer can handle the input."""
        if "spoonacular.com" in url_or_query.lower():
            return True
        return False

    def search(self, query: str) -> List[Dict[str, Any]]:
        """Search for recipes by name/query."""
        if not self.api_key:
            return []
        try:
            response = self.session.get(
                f"{self.API_BASE}/recipes/complexSearch",
                params=self._params(query=query, number=10, addRecipeInformation=True),
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            return [
                {
                    "name": r["title"],
                    "url": f"https://spoonacular.com/recipes/{r.get('title', '').replace(' ', '-').lower()}-{r['id']}",
                    "id": str(r["id"]),
                    "description": self._build_search_description(r),
                    "thumbnail": r.get("image"),
                }
                for r in results
            ]
        except Exception:
            return []

    def _build_search_description(self, recipe: Dict[str, Any]) -> str:
        """Build a description string from search result data."""
        parts = []
        if recipe.get("readyInMinutes"):
            parts.append(f"Ready in {recipe['readyInMinutes']} min")
        if recipe.get("servings"):
            parts.append(f"{recipe['servings']} servings")
        cuisines = recipe.get("cuisines", [])
        if cuisines:
            parts.append(", ".join(cuisines[:2]))
        dish_types = recipe.get("dishTypes", [])
        if dish_types:
            parts.append(", ".join(dish_types[:2]))
        return " | ".join(parts) if parts else ""

    def search_by_ingredients(self, ingredients: str) -> List[Dict[str, Any]]:
        """Search for recipes by ingredients (comma-separated)."""
        if not self.api_key:
            return []
        try:
            response = self.session.get(
                f"{self.API_BASE}/recipes/findByIngredients",
                params=self._params(ingredients=ingredients, number=10, ranking=1),
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            return [
                {
                    "name": r["title"],
                    "url": f"https://spoonacular.com/recipes/{r.get('title', '').replace(' ', '-').lower()}-{r['id']}",
                    "id": str(r["id"]),
                    "description": f"Uses {r.get('usedIngredientCount', 0)} of your ingredients, missing {r.get('missedIngredientCount', 0)}",
                    "thumbnail": r.get("image"),
                }
                for r in data
            ]
        except Exception:
            return []

    def get_random_recipe(self) -> Optional[Dict[str, Any]]:
        """Get a random recipe."""
        if not self.api_key:
            return None
        try:
            response = self.session.get(
                f"{self.API_BASE}/recipes/random",
                params=self._params(number=1),
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            recipes = data.get("recipes", [])
            return recipes[0] if recipes else None
        except Exception:
            return None

    def import_from_url(self, url: str) -> ImportResult:
        """Import a recipe from URL or recipe ID."""
        if not self.api_key:
            return ImportResult(
                success=False,
                error="Spoonacular API key required. Set SPOONACULAR_API_KEY environment variable.",
            )
        try:
            recipe_id = self._extract_recipe_id(url)
            if not recipe_id:
                return ImportResult(
                    success=False,
                    error=f"Could not extract recipe ID from: {url}",
                )

            recipe_data = self._fetch_recipe(recipe_id)
            if not recipe_data:
                return ImportResult(
                    success=False,
                    error=f"Recipe not found: {recipe_id}",
                )

            program = self._convert_to_program(recipe_data)
            return ImportResult(
                success=True,
                program=program,
                source_url=url,
                source_type="spoonacular",
            )

        except Exception as e:
            return ImportResult(success=False, error=str(e))

    def _extract_recipe_id(self, url: str) -> Optional[str]:
        """Extract recipe ID from URL or return if already an ID."""
        if url.isdigit():
            return url

        # Spoonacular URLs: spoonacular.com/recipes/name-12345
        match = re.search(r"(\d+)(?:[/?#]|$)", url)
        if match:
            return match.group(1)

        return None

    def _fetch_recipe(self, recipe_id: str) -> Optional[Dict[str, Any]]:
        """Fetch full recipe info from API."""
        try:
            response = self.session.get(
                f"{self.API_BASE}/recipes/{recipe_id}/information",
                params=self._params(includeNutrition=False),
                timeout=15,
            )
            response.raise_for_status()
            return response.json()
        except Exception:
            return None

    def _determine_task_from_equipment(self, equipment: List[Dict[str, Any]]) -> Optional[str]:
        """Determine task type from step equipment list."""
        for eq in equipment:
            eq_name = eq.get("name", "").lower()
            for key, task in self.EQUIPMENT_TASK_MAP.items():
                if key in eq_name:
                    return task
        return None

    def _determine_task_from_text(self, text: str) -> str:
        """Determine task type from step text using keywords."""
        text_lower = text.lower()
        for task, keywords in self.TASK_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                return task
        return "prep-work"

    def _extract_step_duration(self, step: Dict[str, Any]) -> int:
        """Extract duration in seconds from a Spoonacular step."""
        length = step.get("length")
        if length and length.get("number"):
            number = length["number"]
            unit = length.get("unit", "minutes").lower()
            if "hour" in unit:
                return number * 3600
            elif "second" in unit:
                return max(30, number)
            else:  # minutes
                return number * 60

        # Fall back to text-based estimation
        text = step.get("step", "")
        patterns = [
            (r"(\d+)\s*(?:hours?|hrs?)", lambda m: int(m.group(1)) * 3600),
            (r"(\d+)\s*(?:minutes?|mins?)", lambda m: int(m.group(1)) * 60),
            (r"(\d+)\s*(?:seconds?|secs?)", lambda m: int(m.group(1))),
            (r"(\d+)-(\d+)\s*min", lambda m: (int(m.group(1)) + int(m.group(2))) // 2 * 60),
        ]
        for pattern, extractor in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    return max(60, extractor(match))
                except Exception:
                    continue

        # Default by task type
        task = self._determine_task_from_text(text)
        defaults = {
            "stove-burner": 300,
            "oven": 1200,
            "grill": 600,
            "prep-work": 180,
            "microwave": 120,
            "refrigeration": 1800,
            "waiting": 600,
        }
        return defaults.get(task, 180)

    def _convert_to_program(self, recipe: Dict[str, Any]) -> Dict[str, Any]:
        """Convert Spoonacular recipe data to a Rhylthyme program."""
        name = recipe.get("title", "Unknown Recipe")
        recipe_id = recipe.get("id", "")
        image = recipe.get("image", "")
        servings = recipe.get("servings")
        ready_in = recipe.get("readyInMinutes")
        cuisines = recipe.get("cuisines", [])
        dish_types = recipe.get("dishTypes", [])
        diets = recipe.get("diets", [])
        source_url = recipe.get("sourceUrl", f"https://spoonacular.com/recipes/{recipe_id}")

        # Extract ingredients
        ingredients = []
        for ing in recipe.get("extendedIngredients", []):
            ingredients.append({
                "name": ing.get("name", ""),
                "measure": ing.get("original", ""),
                "amount": ing.get("amount"),
                "unit": ing.get("unit", ""),
            })

        # Build description
        desc_parts = [f"Recipe for {name}"]
        if cuisines:
            desc_parts.append(f"Cuisine: {', '.join(cuisines)}")
        if dish_types:
            desc_parts.append(f"Type: {', '.join(dish_types[:3])}")
        if servings:
            desc_parts.append(f"Servings: {servings}")
        if ready_in:
            desc_parts.append(f"Ready in {ready_in} minutes")
        if ingredients:
            short_list = ", ".join(i["name"] for i in ingredients[:5])
            if len(ingredients) > 5:
                short_list += f" and {len(ingredients) - 5} more"
            desc_parts.append(f"Ingredients: {short_list}")

        description = ". ".join(desc_parts)

        # Create base program
        program = self.create_base_program(
            name=name,
            description=description,
            environment_type="kitchen",
            source_url=source_url,
            source_type="spoonacular",
        )

        # Home cook: can monitor a couple things at once but not 8
        program["actors"] = 2

        # Add metadata
        program["metadata"]["cuisines"] = cuisines
        program["metadata"]["dishTypes"] = dish_types
        program["metadata"]["diets"] = diets
        program["metadata"]["ingredients"] = ingredients
        if servings:
            program["metadata"]["servings"] = servings
        if ready_in:
            program["metadata"]["readyInMinutes"] = ready_in
        if image:
            program["metadata"]["thumbnail"] = image

        # Parse analyzed instructions into steps
        analyzed = recipe.get("analyzedInstructions", [])
        track_steps = []
        step_counter = 0

        for section in analyzed:
            for step in section.get("steps", []):
                step_counter += 1
                step_text = step.get("step", "").strip()
                if not step_text:
                    continue

                equipment = step.get("equipment", [])
                step_ingredients = step.get("ingredients", [])

                # Determine task from equipment first, then text
                task = self._determine_task_from_equipment(equipment) or self._determine_task_from_text(step_text)

                # Extract duration
                duration = self._extract_step_duration(step)

                step_id = f"step_{step_counter:02d}"
                step_name = self.make_step_name(step_text)

                step_data = {
                    "stepId": step_id,
                    "name": step_name,
                    "description": step_text,
                    "task": task,
                    "duration": {
                        "type": "variable",
                        "minSeconds": max(60, duration // 2),
                        "maxSeconds": duration * 2,
                        "defaultSeconds": duration,
                    },
                }

                # Add equipment as notes
                if equipment:
                    eq_names = [e.get("name", "") for e in equipment if e.get("name")]
                    if eq_names:
                        step_data["equipment"] = eq_names

                # Add step ingredients
                if step_ingredients:
                    ing_names = [i.get("name", "") for i in step_ingredients if i.get("name")]
                    if ing_names:
                        step_data["stepIngredients"] = ing_names

                # Set start trigger
                if step_counter == 1 and ingredients:
                    # First cooking step depends on prep completing
                    step_data["startTrigger"] = {
                        "type": "afterStep",
                        "stepId": "prep_ingredients",
                    }
                elif step_counter == 1:
                    step_data["startTrigger"] = {"type": "programStart"}
                else:
                    step_data["startTrigger"] = {
                        "type": "afterStep",
                        "stepId": f"step_{step_counter - 1:02d}",
                    }

                track_steps.append(step_data)

        # If no analyzed instructions, fall back to raw instructions text
        if not track_steps:
            raw_instructions = recipe.get("instructions", "")
            if raw_instructions:
                track_steps = self._parse_raw_instructions(raw_instructions)

        # If still no steps, create a placeholder
        if not track_steps:
            track_steps.append({
                "stepId": "step_01",
                "name": "Prepare meal",
                "description": "Follow recipe instructions",
                "task": "prep-work",
                "duration": {"type": "fixed", "seconds": (ready_in or 30) * 60},
                "startTrigger": {"type": "programStart"},
            })

        # Patch: ensure first cooking step depends on prep (for fallback paths)
        if track_steps and ingredients and track_steps[0].get("startTrigger", {}).get("type") == "programStart":
            track_steps[0]["startTrigger"] = {
                "type": "afterStep",
                "stepId": "prep_ingredients",
            }

        # Build prep track for ingredients
        prep_steps = []
        if ingredients:
            prep_steps.append({
                "stepId": "prep_ingredients",
                "name": "Gather and prepare ingredients",
                "description": "Gather all ingredients: " + ", ".join(
                    i["measure"] if i["measure"] else i["name"]
                    for i in ingredients
                ),
                "task": "prep-work",
                "duration": {"type": "fixed", "seconds": 300},
                "startTrigger": {"type": "programStart"},
            })

        # Build tracks
        program["tracks"] = []

        if prep_steps:
            program["tracks"].append({
                "trackId": "prep",
                "name": "Preparation",
                "description": "Ingredient preparation",
                "steps": prep_steps,
            })

        program["tracks"].append({
            "trackId": "cooking",
            "name": "Cooking Steps",
            "description": f"Main cooking steps for {name}",
            "steps": track_steps,
        })

        # Resource constraints
        program["resourceConstraints"] = [
            {"task": "stove-burner", "maxConcurrent": 4, "description": "Stove burners"},
            {"task": "oven", "maxConcurrent": 1, "description": "Oven"},
            {"task": "grill", "maxConcurrent": 1, "description": "Grill"},
            {"task": "prep-work", "maxConcurrent": 2, "description": "Prep workspace"},
            {"task": "microwave", "maxConcurrent": 1, "description": "Microwave"},
            {"task": "refrigeration", "maxConcurrent": 1, "description": "Refrigerator space"},
            {"task": "waiting", "maxConcurrent": 4, "description": "Passive waiting"},
        ]

        return program

    def _parse_raw_instructions(self, instructions: str) -> List[Dict[str, Any]]:
        """Parse raw instruction text into steps (fallback)."""
        # Split by numbered steps or sentences
        numbered = re.split(r"(?:^|\n)\s*(?:\d+[.):]|\-|\*)\s*", instructions)
        numbered = [s.strip() for s in numbered if s.strip()]

        if len(numbered) <= 1:
            numbered = re.split(r"(?<=[.!])\s+(?=[A-Z])", instructions)
            numbered = [s.strip() for s in numbered if s.strip()]

        steps = []
        for i, text in enumerate(numbered, 1):
            if len(text) < 5:
                continue

            task = self._determine_task_from_text(text)
            duration = self._extract_step_duration({"step": text})

            step_id = f"step_{i:02d}"
            steps.append({
                "stepId": step_id,
                "name": self.make_step_name(text),
                "description": text,
                "task": task,
                "duration": {
                    "type": "variable",
                    "minSeconds": max(60, duration // 2),
                    "maxSeconds": duration * 2,
                    "defaultSeconds": duration,
                },
                "startTrigger": (
                    {"type": "programStart"} if i == 1
                    else {"type": "afterStep", "stepId": f"step_{i - 1:02d}"}
                ),
            })

        return steps


# Register the importer
ImporterRegistry.register(SpoonacularImporter())
