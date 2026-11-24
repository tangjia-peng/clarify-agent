import sys
from pathlib import Path
import unittest

from src.mcp_client import MCPClient


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_subprocess_discovery_call_and_close(self):
        fixture = str(Path(__file__).with_name("mcp_fixture.py"))
        clients = [MCPClient("first", sys.executable, [fixture]), MCPClient("second", sys.executable, [fixture])]
        opened = []
        try:
            for client in clients:
                await client.init()
                opened.append(client)
                self.assertEqual(client.get_tools()[0]["name"], "echo")
                result = await client.call_tool("echo", {"text": "中文测试"})
                self.assertEqual(result["content"][0]["text"], "中文测试")
        finally:
            for client in reversed(opened):
                await client.close()
                await client.close()
                self.assertIsNone(client.session)


if __name__ == "__main__":
    unittest.main()
