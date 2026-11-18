"""Local MCP client and server for catalog and knowledge tools."""

from shopassist.mcp.client import MCPClient
from shopassist.mcp.server import serve_on_fds

__all__ = ["MCPClient", "serve_on_fds"]
