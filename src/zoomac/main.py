"""Zoomac entry point."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add memgate submodule to path
_memgate_path = str(Path(__file__).resolve().parent.parent.parent / "memgate")
if _memgate_path not in sys.path:
    sys.path.insert(0, _memgate_path)

from zoomac.core.config import load_settings
from zoomac.core.loop import CoreLoop


def main() -> None:
    settings = load_settings()
    core = CoreLoop(settings)

    try:
        asyncio.run(core.run_cli())
    except KeyboardInterrupt:
        pass
    finally:
        core.shutdown()


if __name__ == "__main__":
    main()
