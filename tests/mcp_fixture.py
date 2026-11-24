"""仅供本地集成测试的 MCP 服务端。"""

from mcp.server.fastmcp import FastMCP

server = FastMCP("migration-test")


@server.tool()
def echo(text: str) -> str:
    """原样返回文本。"""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
