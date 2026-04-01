"""MemGate daemon — long-running process for fast memory operations.

Usage:
    python memgate_daemon.py <project_dir>

Protocol (stdin/stdout, one JSON per line):
    Request:  {"id": "req_1", "command": "search", "args": {"query": "..."}}
    Response: {"id": "req_1", "results": [...]}
    Startup:  {"type": "ready"}
"""

import json
import os
import sys
from pathlib import Path


def _setup_memgate_path(project_dir: str) -> None:
    """Try to find memgate — bundled copy first, then external."""
    bundled = Path(__file__).resolve().parent
    if (bundled / "memgate" / "__init__.py").exists():
        sys.path.insert(0, str(bundled))
        return

    env_path = os.environ.get("MEMGATE_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            sys.path.insert(0, str(p))
            return

    candidates = [
        Path(project_dir) / "memgate",
        Path(project_dir).parent.parent / "memgate",
        Path(project_dir).parent / "memgate",
    ]
    for candidate in candidates:
        if candidate.exists() and (candidate / "__init__.py").exists():
            sys.path.insert(0, str(candidate.parent))
            return


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: memgate_daemon.py <project_dir>"}))
        sys.exit(1)

    project_dir = sys.argv[1]
    _setup_memgate_path(project_dir)

    try:
        from memgate import MemGate
    except ImportError as e:
        print(json.dumps({"error": f"memgate not found: {e}"}))
        sys.stdout.flush()
        sys.exit(1)

    Path(project_dir).mkdir(parents=True, exist_ok=True)

    # Redirect stderr to suppress model loading noise
    old_stderr = sys.stderr
    sys.stderr = open(os.devnull, "w")

    mg = MemGate(project_dir=project_dir)

    sys.stderr = old_stderr

    # Signal ready
    print(json.dumps({"type": "ready"}))
    sys.stdout.flush()

    # Process requests from stdin
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = req.get("id", "unknown")
        command = req.get("command", "")
        args = req.get("args", {})

        try:
            result = {"id": req_id}

            if command == "retrieve":
                ctx = mg.context(args.get("query", ""), max_tokens=args.get("max_tokens", 2000))
                result["context"] = ctx.get("context", "")

            elif command == "search":
                results = mg.search(args.get("query", ""), top_k=args.get("top_k", 10))
                result["results"] = results

            elif command == "store":
                payload = {"content": args.get("content", "")}
                if args.get("entities"):
                    payload["entities"] = args["entities"]
                if args.get("relationships"):
                    payload["relationships"] = args["relationships"]
                r = mg.ingest_structured(payload)
                result["stored"] = True
                result["result"] = str(r)

            elif command == "facts":
                facts = mg.facts(entity=args.get("entity"))
                result["facts"] = facts

            elif command == "status":
                status = mg.status()
                result["status"] = status

            else:
                result["error"] = f"Unknown command: {command}"

            print(json.dumps(result))
            sys.stdout.flush()

        except Exception as e:
            print(json.dumps({"id": req_id, "error": str(e)}))
            sys.stdout.flush()


if __name__ == "__main__":
    main()
