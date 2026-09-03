"""Not part of the pytest suite — a manual end-to-end smoke test that spawns
the server as a real MCP stdio subprocess and drives it with the actual
client SDK, to sanity check the FastMCP wiring end-to-end.

Usage: python tests/manual_smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "homelab_deploy_mcp.server"],
        env={**os.environ, "HOMELAB_DEPLOY_MCP_CONFIG": str(REPO_ROOT / "config.yaml")},
        cwd=str(REPO_ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("Tools exposed:")
            for tool in tools.tools:
                print(f"  - {tool.name}: {tool.description!r}")
                print(f"    input_schema: {tool.input_schema}")

            print("\nCalling with an unconfigured target (should fail cleanly):")
            result = await session.call_tool(
                "redeploy",
                {"target": "not-a-real-target", "branch": "main", "env_file": "prod.env"},
            )
            print(f"  is_error={result.is_error} content={result.content}")

            print("\nCalling with a disallowed branch (should fail cleanly):")
            result = await session.call_tool(
                "redeploy",
                {
                    "target": "mediaclipmakarr",
                    "branch": "totally-not-a-real-branch",
                    "env_file": "prod.env",
                },
            )
            print(f"  is_error={result.is_error} content={result.content}")

            print("\nCalling with an allowed target/branch (SSH connect will fail since")
            print("192.168.1.50 is a placeholder host - that's expected here):")
            result = await session.call_tool(
                "redeploy",
                {"target": "mediaclipmakarr", "branch": "main", "env_file": "prod.env"},
            )
            print(f"  is_error={result.is_error} content={result.content}")

            print("\nCalling with a branch matching the codex/* glob pattern (should pass")
            print("allowlist validation and reach the same SSH-connect-failure stage):")
            result = await session.call_tool(
                "redeploy",
                {"target": "mediaclipmakarr", "branch": "codex/some-feature", "env_file": "prod.env"},
            )
            print(f"  is_error={result.is_error} content={result.content}")


if __name__ == "__main__":
    asyncio.run(main())
