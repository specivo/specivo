"""Run MCP server in stdio mode: python -m specivo.mcp"""

from specivo.mcp.server import mcp

if __name__ == "__main__":
    mcp.run()
