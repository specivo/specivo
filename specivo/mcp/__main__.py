"""MCP server entry point -- HTTP transport only.

The MCP server is mounted inside the FastAPI application and does not
run as a standalone process.  Agents connect via:

  Streamable HTTP:  POST http://localhost:8030/mcp/
  SSE:              GET  http://localhost:8030/mcp/sse

Both require ``Authorization: Bearer spv_...`` header.

This module is kept so that ``python -m specivo.mcp`` prints a helpful
message instead of silently failing.
"""

import sys


def main() -> None:
    print(
        "The MCP server is embedded in the Specivo FastAPI application.\n"
        "Start the app with: uvicorn specivo.main:app --port 8030\n"
        "\n"
        "Endpoints:\n"
        "  Streamable HTTP:  POST http://localhost:8030/mcp/\n"
        "  SSE:              GET  http://localhost:8030/mcp/sse\n"
        "\n"
        "Both require: Authorization: Bearer spv_...",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
