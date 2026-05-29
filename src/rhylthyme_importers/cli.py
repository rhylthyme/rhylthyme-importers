"""
Command-line interface for Rhylthyme importers.
"""

import argparse
import json
import sys
from typing import Optional

from . import ImporterRegistry, TheMealDBImporter, ProtocolsIOImporter, SpoonacularImporter


def main():
    parser = argparse.ArgumentParser(
        description="Import external data sources as Rhylthyme programs"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # List command
    list_parser = subparsers.add_parser("list", help="List available importers")

    # Import command
    import_parser = subparsers.add_parser("import", help="Import from URL")
    import_parser.add_argument("url", help="URL or ID to import")
    import_parser.add_argument(
        "-o", "--output",
        help="Output file (default: stdout)"
    )
    import_parser.add_argument(
        "-i", "--importer",
        help="Specific importer to use (auto-detected if not specified)"
    )
    import_parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty print JSON output"
    )

    # Search command
    search_parser = subparsers.add_parser("search", help="Search for importable items")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument(
        "-i", "--importer",
        required=True,
        help="Importer to use for search"
    )

    # MealDB specific commands
    mealdb_parser = subparsers.add_parser("mealdb", help="TheMealDB specific commands")
    mealdb_sub = mealdb_parser.add_subparsers(dest="mealdb_cmd")

    random_parser = mealdb_sub.add_parser("random", help="Import a random meal")
    random_parser.add_argument("-o", "--output", help="Output file")
    random_parser.add_argument("--pretty", action="store_true")

    categories_parser = mealdb_sub.add_parser("categories", help="List meal categories")

    args = parser.parse_args()

    if args.command == "list":
        list_importers()
    elif args.command == "import":
        do_import(args.url, args.output, args.importer, args.pretty)
    elif args.command == "search":
        do_search(args.query, args.importer)
    elif args.command == "mealdb":
        if args.mealdb_cmd == "random":
            import_random_meal(args.output, args.pretty)
        elif args.mealdb_cmd == "categories":
            list_meal_categories()
        else:
            mealdb_parser.print_help()
    else:
        parser.print_help()


def list_importers():
    """List all available importers."""
    importers = ImporterRegistry.list_importers()
    print("Available importers:\n")
    for imp in importers:
        print(f"  {imp['name']}")
        print(f"    Description: {imp['description']}")
        print(f"    Domains: {', '.join(imp['supported_domains'])}")
        print()


def do_import(url: str, output: Optional[str], importer_name: Optional[str], pretty: bool):
    """Import from URL."""
    # Find importer
    if importer_name:
        importer = ImporterRegistry.get(importer_name)
        if not importer:
            print(f"Error: Unknown importer '{importer_name}'", file=sys.stderr)
            print("Use 'rhylthyme-import list' to see available importers", file=sys.stderr)
            sys.exit(1)
    else:
        importer = ImporterRegistry.find_for_url(url)
        if not importer:
            print(f"Error: No importer found for URL: {url}", file=sys.stderr)
            sys.exit(1)

    print(f"Using importer: {importer.name}", file=sys.stderr)

    # Do import
    result = importer.import_from_url(url)

    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)

    # Output
    json_str = json.dumps(result.program, indent=2 if pretty else None, ensure_ascii=False)

    if output:
        with open(output, "w") as f:
            f.write(json_str)
        print(f"Saved to: {output}", file=sys.stderr)
    else:
        print(json_str)


def do_search(query: str, importer_name: str):
    """Search for importable items."""
    importer = ImporterRegistry.get(importer_name)
    if not importer:
        print(f"Error: Unknown importer '{importer_name}'", file=sys.stderr)
        sys.exit(1)

    results = importer.search(query)

    if not results:
        print("No results found.")
        return

    print(f"Found {len(results)} results:\n")
    for item in results:
        print(f"  Name: {item.get('name', 'Unknown')}")
        if item.get("description"):
            print(f"  Description: {item['description'][:100]}")
        print(f"  URL: {item.get('url', 'N/A')}")
        print()


def import_random_meal(output: Optional[str], pretty: bool):
    """Import a random meal from TheMealDB."""
    importer = TheMealDBImporter()
    meal = importer.get_random_meal()

    if not meal:
        print("Error: Failed to get random meal", file=sys.stderr)
        sys.exit(1)

    meal_id = meal.get("idMeal")
    result = importer.import_from_url(meal_id)

    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)

    json_str = json.dumps(result.program, indent=2 if pretty else None, ensure_ascii=False)

    if output:
        with open(output, "w") as f:
            f.write(json_str)
        print(f"Saved: {result.program['name']} to {output}", file=sys.stderr)
    else:
        print(json_str)


def list_meal_categories():
    """List TheMealDB categories."""
    importer = TheMealDBImporter()
    categories = importer.get_categories()

    if not categories:
        print("Error: Failed to get categories", file=sys.stderr)
        sys.exit(1)

    print("Available meal categories:\n")
    for cat in categories:
        print(f"  - {cat}")


if __name__ == "__main__":
    main()
