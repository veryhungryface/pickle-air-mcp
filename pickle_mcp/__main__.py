"""Only stdio is exposed. There is no HTTP port, remote host or raw ADB tool."""
import argparse
import asyncio
from pathlib import Path
import sys


def parser():
    p = argparse.ArgumentParser(description="Unofficial local Pickle stdio MCP server")
    p.add_argument("--agent", action="append", required=True, help="Allowed exact agent name; repeat for multiple names")
    p.add_argument("--port", type=int, default=5555, help="Loopback ADB port, never an MCP listening port")
    p.add_argument("--state-dir", type=Path, default=Path("~/.local/state/pickle-air-cli"))
    p.add_argument("--upload-dir", type=Path, help="Dedicated folder containing explicitly shareable attachments")
    p.add_argument("--download-dir", type=Path, default=Path("~/Downloads/pickle-air-mcp"))
    p.add_argument("--enable-ask", action="store_true", help="Allow real sends; keep host approval enabled")
    p.add_argument("--enable-call", action="store_true", help="Allow pickle_call (real phone calls); also needs --enable-ask")
    p.add_argument("--process-timeout", type=int, default=1800, help="Whole CLI time limit, 60–7200 seconds")
    return p


async def serve(settings):
    from mcp.server.stdio import stdio_server
    from .server import create_server
    server = create_server(settings)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv=None):
    args = parser().parse_args(argv)
    if sys.platform not in {"darwin", "linux"}:
        print("Supported runtime: macOS. Linux is for mock tests only.", file=sys.stderr)
        return 2
    from .server import Settings
    try:
        settings = Settings(agents=tuple(args.agent), port=args.port, state_dir=args.state_dir,
                            upload_dir=args.upload_dir, download_dir=args.download_dir,
                            enable_ask=args.enable_ask, enable_call=args.enable_call, process_timeout=args.process_timeout)
    except (ValueError, OSError):
        print("Invalid local configuration. Check agent, port and directory settings.", file=sys.stderr)
        return 2
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
