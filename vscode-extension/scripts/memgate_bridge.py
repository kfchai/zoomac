"""MemGate bridge for VS Code extension subprocess calls.

Usage:
    python memgate_bridge.py <project_dir> <command> [args_json]

Commands:
    retrieve <query_json>     — Retrieve context for a query
    search <query_json>       — Search memories by similarity
    store <payload_json>      — Ingest content into memory
    facts <args_json>         — Look up facts about an entity
    status                    — Memory system status

Output: JSON on stdout, errors on stderr.
"""

import json
import os
import sys
from pathlib import Path


def _setup_memgate_path(project_dir: str) -> None:
    """Try to find memgate — bundled copy first, then external."""
    # 0. Bundled copy inside the extension's scripts/ directory (always available)
    bundled = Path(__file__).resolve().parent
    if (bundled / "memgate" / "__init__.py").exists():
        sys.path.insert(0, str(bundled))
        return

    # 1. MEMGATE_PATH env var (set by extension from settings)
    env_path = os.environ.get("MEMGATE_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            sys.path.insert(0, str(p))
            return

    candidates = [
        # 2. memgate submodule in the workspace project
        Path(project_dir) / "memgate",
        # 3. memgate submodule in workspace root (project_dir might be .zoomac/memory)
        Path(project_dir).parent.parent / "memgate",
        # 4. pip installed — already on sys.path, no action needed
    ]
    for candidate in candidates:
        if candidate.exists() and (candidate / "__init__.py").exists():
            sys.path.insert(0, str(candidate.parent))
            return


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: memgate_bridge.py <project_dir> <command> [args_json]"}))
        sys.exit(1)

    project_dir = sys.argv[1]
    command = sys.argv[2]
    args_json = sys.argv[3] if len(sys.argv) > 3 else "{}"

    # Read from stdin if args is "-"
    if args_json == "-":
        args_json = sys.stdin.read().strip() or "{}"

    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON args: {e}"}))
        sys.exit(1)

    # Try to find memgate
    _setup_memgate_path(project_dir)

    try:
        from memgate import MemGate
    except ImportError as e:
        # Debug: show where we looked
        script_dir = str(Path(__file__).resolve().parent)
        bundled = str(Path(__file__).resolve().parent / "memgate")
        print(json.dumps({
            "error": f"memgate module not found. "
                     f"Script: {script_dir}, "
                     f"Bundled exists: {Path(bundled).exists()}, "
                     f"sys.path[0:3]: {sys.path[0:3]}, "
                     f"ImportError: {e}"
        }))
        sys.exit(1)

    try:
        # Ensure the memory directory exists
        Path(project_dir).mkdir(parents=True, exist_ok=True)
        mg = MemGate(project_dir=project_dir)

        if command == "retrieve":
            query = args.get("query", "")
            max_tokens = args.get("max_tokens", 2000)
            result = mg.context(query, max_tokens=max_tokens)
            print(json.dumps({"context": result.get("context", "")}))

        elif command == "search":
            query = args.get("query", "")
            top_k = args.get("top_k", 10)
            results = mg.search(query, top_k=top_k)
            print(json.dumps({"results": results}))

        elif command == "store":
            content = args.get("content", "")
            entities = args.get("entities")
            relationships = args.get("relationships")
            payload = {"content": content}
            if entities:
                payload["entities"] = entities
            if relationships:
                payload["relationships"] = relationships
            result = mg.ingest_structured(payload)
            print(json.dumps({"stored": True, "result": str(result)}))

        elif command == "facts":
            entity = args.get("entity")
            facts = mg.facts(entity=entity)
            print(json.dumps({"facts": facts}))

        elif command == "status":
            status = mg.status()
            print(json.dumps({"status": status}))

        else:
            print(json.dumps({"error": f"Unknown command: {command}"}))
            sys.exit(1)

        mg.close()

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
