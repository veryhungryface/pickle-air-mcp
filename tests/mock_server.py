"""Test-only process: real MCP stdio, fake CLI, no ADB or network."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp.server.stdio import stdio_server
from pickle_mcp.server import Settings, create_server

root = Path(sys.argv[1])

def fake_cli(argv, timeout):
    # Synthetic test inputs only. This log is never produced by the real server.
    with (root / "calls.jsonl").open("a") as f:
        f.write(json.dumps(argv) + "\n")
    command = argv[4]
    if command == "doctor":
        return 0, {"mock": True, "endpoint": "127.0.0.1:5555"}
    if command == "read":
        return 0, {"mock": True, "messages": [{"text": "synthetic reply"}], "scope": "currently visible chat only"}
    if command == "ask":
        message = next(a.split("=", 1)[1] for a in argv if a.startswith("--message="))
        if message == "simulate uncertainty":
            return 4, {"mock": True, "state": "uncertain", "sent_confirmed": False}
        return 0, {"mock": True, "state": "completed", "sent_confirmed": True, "replies": ["synthetic reply"]}
    if command == "download":
        name = argv[argv.index("--name") + 1]
        output = Path(argv[argv.index("--output-dir") + 1]) / name
        output.write_bytes(b"synthetic file\n")
        return 0, {"mock": True, "path": str(output), "bytes": 15,
                   "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    raise AssertionError(command)

async def main():
    server = create_server(Settings(agents=("Synthetic Agent",), state_dir=root / "state",
        upload_dir=root / "upload", download_dir=root / "downloads", enable_ask=True), fake_cli)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

asyncio.run(main())
