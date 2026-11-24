"""使用官方 MCP SDK，通过 stdio 启动并连接外部服务端。"""

from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
import shutil
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Implementation


class MCPClient:
    def __init__(
        self, name: str, command: str, args: list[str],
        version: str = "0.0.1", cwd: Path | None = None,
    ) -> None:
        self.name = name
        self.command = command
        self.args = args
        self.version = version
        self.cwd = cwd
        self.session: ClientSession | None = None
        self.tools: list[dict[str, Any]] = []
        self._stack = AsyncExitStack()

    async def init(self) -> None:
        if self.session is not None:
            return
        command = shutil.which(self.command)
        if not command:
            raise FileNotFoundError(f"找不到命令 {self.command}，请先安装并加入 PATH")
        params = StdioServerParameters(
            command=command, args=self.args, cwd=self.cwd,
            env={"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self.session = await self._stack.enter_async_context(ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=120),
                client_info=Implementation(name=self.name, version=self.version),
            ))
            await self.session.initialize()
            cursor = None
            while True:
                result = await self.session.list_tools(cursor=cursor)
                self.tools.extend({
                    "name": tool.name, "description": tool.description,
                    "inputSchema": tool.inputSchema,
                } for tool in result.tools)
                cursor = result.nextCursor
                if not cursor:
                    break
            print(f"Connected to server {self.name} with tools:", [t["name"] for t in self.tools])
        except BaseException:
            await self.close()
            raise

    async def call_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.session is None:
            raise RuntimeError("MCP 客户端尚未初始化")
        result = await self.session.call_tool(name, arguments=params)
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    def get_tools(self) -> list[dict[str, Any]]:
        return list(self.tools)

    async def close(self) -> None:
        try:
            await self._stack.aclose()
        finally:
            self.session = None
            self.tools = []
