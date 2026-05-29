"""
TheMealDB Importer - Import recipes from TheMealDB API as Rhylthyme cooking programs.

API Documentation: https://www.themealdb.com/api.php
"""

import requests
import re
from typing import Dict, Any, List, Optional
from .base import BaseImporter, ImportResult, ImporterRegistry


class TheMealDBImporter(BaseImporter):
    """Import recipes from TheMealDB API."""

    name = "themealdb"
    description = "Import recipes from TheMealDB (free recipe API)"
    supported_domains = ["themealdb.com"]

    API_BASE = "https://www.themealdb.com/api/json/v1/1"

    # Cooking task mappings based on instruction keywords
    TASK_MAPPINGS = {
        "stove-burner": [
            "fry", "sauté", "saute", "simmer", "boil", "cook on", "heat",
            "pan", "skillet", "wok", "pot", "saucepan", "reduce"
        ],
        "oven": [
            "bake", "roast", "broil", "oven", "grill", "toast"
        ],
        "prep-work": [
            "chop", "dice", "slice", "mince", "cut", "peel", "grate",
            "mix", "combine", "stir", "whisk", "beat", "fold", "season",
            "marinate", "coat", "dredge", "prepare", "arrange"
        ],
        "microwave": [
            "microwave", "nuke"
        ],
        "refrigeration": [
            "refrigerate", "chill", "cool", "rest in fridge"
        ],
        "waiting": [
            "rest", "stand", "wait", "let sit", "rise", "proof"
        ]
    }

    # Duration estimation based on keywords (in seconds)
    DURATION_HINTS = {
        # Cooking methods with typical durations
        r"boil.*?(\d+)\s*min": lambda m: int(m.group(1)) * 60,
        r"simmer.*?(\d+)\s*min": lambda m: int(m.group(1)) * 60,
        r"bake.*?(\d+)\s*min": lambda m: int(m.group(1)) * 60,
        r"fry.*?(\d+)\s*min": lambda m: int(m.group(1)) * 60,
        r"cook.*?(\d+)\s*min": lambda m: int(m.group(1)) * 60,
        r"(\d+)\s*minutes?": lambda m: int(m.group(1)) * 60,
        r"(\d+)\s*hours?": lambda m: int(m.group(1)) * 3600,
        r"(\d+)-(\d+)\s*min": lambda m: (int(m.group(1)) + int(m.group(2))) // 2 * 60,
        # Keywords with typical durations
        r"\bquickly\b": lambda m: 60,
        r"\bbriefly\b": lambda m: 30,
        r"\buntil golden\b": lambda m: 300,
        r"\buntil tender\b": lambda m: 600,
        r"\buntil done\b": lambda m: 600,
    }

    def __init__(self, api_key: str = "1"):
        """Initialize with API key (default is test key "1")."""
        self.api_key = api_key
        self.session = requests.Session()

    def can_import(self, url_or_query: str) -> bool:
        """Check if this importer can handle the input."""
        if "themealdb.com" in url_or_query.lower():
            return True
        # Also accept meal IDs
        if url_or_query.isdigit():
            return True
        return False

    def search(self, query: str) -> List[Dict[str, Any]]:
        """Search for meals by name."""
        try:
            response = self.session.get(
                f"{self.API_BASE}/search.php",
                params={"s": query},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            meals = data.get("meals") or []
            return [
                {
                    "name": meal["strMeal"],
                    "url": f"https://www.themealdb.com/meal/{meal['idMeal']}",
                    "id": meal["idMeal"],
                    "description": f"{meal.get('strCategory', '')} - {meal.get('strArea', '')} cuisine",
                    "thumbnail": meal.get("strMealThumb")
                }
                for meal in meals
            ]
        except Exception as e:
            return []

    def search_by_category(self, category: str) -> List[Dict[str, Any]]:
        """Search for meals by category."""
        try:
            response = self.session.get(
                f"{self.API_BASE}/filter.php",
                params={"c": category},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            meals = data.get("meals") or []
            return [
                {
                    "name": meal["strMeal"],
                    "url": f"https://www.themealdb.com/meal/{meal['idMeal']}",
                    "id": meal["idMeal"],
                    "thumbnail": meal.get("strMealThumb")
                }
                for meal in meals
            ]
        except Exception:
            return []

    def get_random_meal(self) -> Optional[Dict[str, Any]]:
        """Get a random meal."""
        try:
            response = self.session.get(f"{self.API_BASE}/random.php", timeout=10)
            response.raise_for_status()
            data = response.json()
            meals = data.get("meals") or []
            return meals[0] if meals else None
        except Exception:
            return None

    def get_categories(self) -> List[str]:
        """Get all available meal categories."""
        try:
            response = self.session.get(f"{self.API_BASE}/categories.php", timeout=10)
            response.raise_for_status()
            data = response.json()
            categories = data.get("categories") or []
            return [cat["strCategory"] for cat in categories]
        except Exception:
            return []

    def import_from_url(self, url: str) -> ImportResult:
        """Import a meal from URL or meal ID."""
        try:
            # Extract meal ID from URL or use directly if numeric
            meal_id = self._extract_meal_id(url)
            if not meal_id:
                return ImportResult(
                    success=False,
                    error=f"Could not extract meal ID from: {url}"
                )

            # Fetch meal data
            meal_data = self._fetch_meal(meal_id)
            if not meal_data:
                return ImportResult(
                    success=False,
                    error=f"Meal not found: {meal_id}"
                )

            # Convert to Rhylthyme program
            program = self._convert_to_program(meal_data)

            return ImportResult(
                success=True,
                program=program,
                source_url=url,
                source_type="themealdb"
            )

        except Exception as e:
            return ImportResult(
                success=False,
                error=str(e)
            )

    def _extract_meal_id(self, url: str) -> Optional[str]:
        """Extract meal ID from URL or return if already an ID."""
        if url.isdigit():
            return url

        # Try common URL patterns
        patterns = [
            r"themealdb\.com/meal/(\d+)",
            r"idMeal[=:](\d+)",
            r"/(\d+)(?:[/?#]|$)"
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)

        return None

    def _fetch_meal(self, meal_id: str) -> Optional[Dict[str, Any]]:
        """Fetch meal details from API."""
        try:
            response = self.session.get(
                f"{self.API_BASE}/lookup.php",
                params={"i": meal_id},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            meals = data.get("meals") or []
            return meals[0] if meals else None
        except Exception:
            return None

    def _extract_ingredients(self, meal: Dict[str, Any]) -> List[Dict[str, str]]:
        """Extract ingredients list from meal data."""
        ingredients = []
        for i in range(1, 21):
            ingredient = (meal.get(f"strIngredient{i}") or "").strip()
            measure = (meal.get(f"strMeasure{i}") or "").strip()
            if ingredient:
                ingredients.append({
                    "name": ingredient,
                    "measure": measure
                })
        return ingredients

    def _parse_instructions(self, instructions: str) -> List[Dict[str, Any]]:
        """Parse instruction text into steps."""
        if not instructions:
            return []

        # Split by common delimiters
        # Try numbered steps first
        numbered = re.split(r'(?:^|\n)\s*(?:\d+[\.\)\:]|\-|\*)\s*', instructions)
        numbered = [s.strip() for s in numbered if s.strip()]

        if len(numbered) > 1:
            steps = numbered
        else:
            # Fall back to sentence splitting
            steps = re.split(r'(?<=[.!])\s+(?=[A-Z])', instructions)
            steps = [s.strip() for s in steps if s.strip()]

        # Parse each step
        parsed_steps = []
        for i, step_text in enumerate(steps, 1):
            if len(step_text) < 5:  # Skip very short fragments
                continue

            parsed_steps.append({
                "number": i,
                "text": step_text,
                "name": self.make_step_name(step_text),
                "task": self._determine_task(step_text),
                "duration": self._estimate_duration(step_text)
            })

        return parsed_steps

    def _determine_task(self, text: str) -> str:
        """Determine the cooking task type from step text."""
        text_lower = text.lower()

        for task, keywords in self.TASK_MAPPINGS.items():
            if any(kw in text_lower for kw in keywords):
                return task

        return "prep-work"  # Default

    def _estimate_duration(self, text: str) -> int:
        """Estimate step duration in seconds."""
        text_lower = text.lower()

        # Try to extract explicit durations
        for pattern, extractor in self.DURATION_HINTS.items():
            match = re.search(pattern, text_lower)
            if match:
                try:
                    return extractor(match)
                except Exception:
                    continue

        # Default durations by task type
        task = self._determine_task(text)
        default_durations = {
            "stove-burner": 300,   # 5 minutes
            "oven": 1200,          # 20 minutes
            "prep-work": 180,      # 3 minutes
            "microwave": 120,      # 2 minutes
            "refrigeration": 1800, # 30 minutes
            "waiting": 600         # 10 minutes
        }

        return default_durations.get(task, 180)

    def _convert_to_program(self, meal: Dict[str, Any]) -> Dict[str, Any]:
        """Convert meal data to Rhylthyme program."""
        name = meal.get("strMeal", "Unknown Recipe")
        category = meal.get("strCategory", "")
        area = meal.get("strArea", "")
        instructions = meal.get("strInstructions", "")
        meal_id = meal.get("idMeal", "")
        thumbnail = meal.get("strMealThumb", "")
        youtube = meal.get("strYoutube", "")

        # Extract ingredients
        ingredients = self._extract_ingredients(meal)

        # Parse instructions into steps
        parsed_steps = self._parse_instructions(instructions)

        # Build description
        description_parts = [f"Recipe for {name}"]
        if category:
            description_parts.append(f"Category: {category}")
        if area:
            description_parts.append(f"Cuisine: {area}")
        if ingredients:
            ingredient_list = ", ".join(
                f"{i['measure']} {i['name']}" if i['measure'] else i['name']
                for i in ingredients[:5]
            )
            if len(ingredients) > 5:
                ingredient_list += f" and {len(ingredients) - 5} more ingredients"
            description_parts.append(f"Ingredients: {ingredient_list}")

        description = ". ".join(description_parts)

        # Create base program
        program = self.create_base_program(
            name=name,
            description=description,
            environment_type="kitchen",
            source_url=f"https://www.themealdb.com/meal/{meal_id}",
            source_type="themealdb"
        )

        # Home cook: can monitor a couple things at once but not 8
        program["actors"] = 2

        # Add metadata
        program["metadata"]["category"] = category
        program["metadata"]["area"] = area
        program["metadata"]["ingredients"] = ingredients
        if thumbnail:
            program["metadata"]["thumbnail"] = thumbnail
        if youtube:
            program["metadata"]["youtube"] = youtube

        # Create steps for the main track
        track_steps = []
        for i, step in enumerate(parsed_steps):
            step_id = f"step_{i+1:02d}"

            step_data = {
                "stepId": step_id,
                "name": step.get("name", f"Step {step['number']}"),
                "description": step["text"],
                "task": step["task"],
                "duration": {
                    "type": "variable",
                    "minSeconds": max(60, step["duration"] // 2),
                    "maxSeconds": step["duration"] * 2,
                    "defaultSeconds": step["duration"]
                }
            }

            # Set start trigger
            if i == 0:
                step_data["startTrigger"] = {"type": "programStart"}
            else:
                step_data["startTrigger"] = {
                    "type": "afterStep",
                    "stepId": f"step_{i:02d}"
                }

            track_steps.append(step_data)

        # If no steps were parsed, create a single generic step
        if not track_steps:
            track_steps.append({
                "stepId": "step_01",
                "name": "Prepare meal",
                "description": instructions or "Follow recipe instructions",
                "task": "prep-work",
                "duration": {"type": "fixed", "seconds": 1800},
                "startTrigger": {"type": "programStart"}
            })

        # Add prep track for ingredients
        prep_steps = []
        if ingredients:
            prep_steps.append({
                "stepId": "prep_ingredients",
                "name": "Gather and prepare ingredients",
                "description": "Gather all ingredients: " + ", ".join(
                    f"{i['measure']} {i['name']}" if i['measure'] else i['name']
                    for i in ingredients
                ),
                "task": "prep-work",
                "duration": {"type": "fixed", "seconds": 300},
                "startTrigger": {"type": "programStart"}
            })

        # Build tracks
        program["tracks"] = []

        if prep_steps:
            program["tracks"].append({
                "trackId": "prep",
                "name": "Preparation",
                "description": "Ingredient preparation",
                "steps": prep_steps
            })

        program["tracks"].append({
            "trackId": "cooking",
            "name": "Cooking Steps",
            "description": f"Main cooking steps for {name}",
            "steps": track_steps
        })

        # Set resource constraints
        program["resourceConstraints"] = [
            {"task": "stove-burner", "maxConcurrent": 4, "description": "Stove burners"},
            {"task": "oven", "maxConcurrent": 1, "description": "Oven"},
            {"task": "prep-work", "maxConcurrent": 2, "description": "Prep workspace"},
            {"task": "microwave", "maxConcurrent": 1, "description": "Microwave"},
            {"task": "refrigeration", "maxConcurrent": 1, "description": "Refrigerator space"},
            {"task": "waiting", "maxConcurrent": 4, "description": "Passive waiting"}
        ]

        return program


# Register the importer
ImporterRegistry.register(TheMealDBImporter())
