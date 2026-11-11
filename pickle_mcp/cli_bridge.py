"""Launch only the bundled CLI, even when the MCP client's cwd is elsewhere."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pickle_cli.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
