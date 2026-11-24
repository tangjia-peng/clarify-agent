"""【新增】多轮对话回归：验证实际发出的历史、终端循环与退出清理，不访问外部 API。"""

from copy import deepcopy
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from src.agent import Agent
from src.index import main, read_prompt
from test_core import FakeStream, chunk, part


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_turn_sends_history_including_tool_results(self):
        requests = []
        streams = iter([
            FakeStream([chunk(calls=[part(0, "call-1", "lookup", "{}")])]),
            FakeStream([chunk("她叫 Bret。")]),
            FakeStream([chunk("刚才说的是 Bret。")]),
        ])

        async def create(**kwargs):
            requests.append(deepcopy(kwargs))
            return next(streams)

        api = NS(chat=NS(completions=NS(create=AsyncMock(side_effect=create))), close=AsyncMock())
        mcp = NS(
            init=AsyncMock(), close=AsyncMock(),
            get_tools=lambda: [{"name": "lookup", "inputSchema": {"type": "object"}}],
            call_tool=AsyncMock(return_value={"content": [{"type": "text", "text": "Bret"}]}),
        )
        with patch("src.chat_openai.required_env", return_value="test"), patch(
            "src.chat_openai.AsyncOpenAI", return_value=api,
        ):
            agent = Agent("test", [mcp])
            try:
                await agent.init()
                self.assertEqual(await agent.invoke("她叫什么？", context="资料一"), "她叫 Bret。")
                self.assertEqual(await agent.invoke("刚才提到谁？", context="资料二"), "刚才说的是 Bret。")
                api.close.assert_not_awaited()
            finally:
                await agent.close()
        history = requests[2]["messages"]
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "tool", "assistant", "user"])
        self.assertIn("她叫什么？", history[0]["content"])
        self.assertIn("资料一", history[0]["content"])
        self.assertEqual(history[2]["tool_call_id"], "call-1")
        self.assertEqual(history[3]["content"], "她叫 Bret。")
        self.assertIn("资料二", history[4]["content"])
        mcp.init.assert_awaited_once()
        mcp.close.assert_awaited_once()
        api.close.assert_awaited_once()

    async def test_terminal_reuses_agent_and_retriever_after_turn_error(self):
        retriever = NS(retrieve=AsyncMock(side_effect=[["资料一"], ["资料二"], ["资料三"]]))
        agent = NS(
            init=AsyncMock(), close=AsyncMock(),
            invoke=AsyncMock(side_effect=["首轮回答", RuntimeError("断网"), "最后回答"]),
        )
        with patch("src.index.create_retriever", AsyncMock(return_value=retriever)) as build, patch(
            "src.index.Agent", return_value=agent,
        ) as factory, patch("src.index.create_mcp_clients", return_value=[]), patch(
            "builtins.input", side_effect=["", "第二问", "第三问", "/exit"],
        ):
            self.assertEqual(await main("第一问"), "最后回答")
        build.assert_awaited_once()
        factory.assert_called_once()
        agent.init.assert_awaited_once()
        agent.close.assert_awaited_once()
        self.assertEqual([c.args[0] for c in agent.invoke.await_args_list], ["第一问", "第二问", "第三问"])
        self.assertEqual(retriever.retrieve.await_count, 3)

    async def test_no_default_task_and_eof_closes_session(self):
        agent = NS(init=AsyncMock(), close=AsyncMock(), invoke=AsyncMock(return_value="回答"))
        with patch("builtins.input", side_effect=["用户首问", EOFError]), patch(
            "src.index.create_retriever", AsyncMock(return_value=NS(retrieve=AsyncMock(return_value=[])))), patch(
            "src.index.Agent", return_value=agent,
        ), patch("src.index.create_mcp_clients", return_value=[]):
            self.assertEqual(await main(), "回答")
        agent.invoke.assert_awaited_once_with("用户首问", context="")
        agent.close.assert_awaited_once()

    async def test_exit_before_first_question_does_not_initialize(self):
        with patch("builtins.input", return_value="/quit"), patch("src.index.create_retriever") as build:
            self.assertEqual(await main(), "")
        build.assert_not_called()

    async def test_initialization_failure_closes_session(self):
        agent = NS(init=AsyncMock(side_effect=RuntimeError("启动失败")), close=AsyncMock())
        with patch("src.index.create_retriever", AsyncMock()), patch(
            "src.index.create_mcp_clients", return_value=[],
        ), patch("src.index.Agent", return_value=agent):
            with self.assertRaisesRegex(RuntimeError, "启动失败"):
                await main("第一问")
        agent.close.assert_awaited_once()

    async def test_input_eof_and_exit_commands(self):
        for value in ["exit", "QUIT", "/exit", "/quit"]:
            with patch("builtins.input", return_value=value):
                self.assertIsNone(await read_prompt())
        with patch("builtins.input", side_effect=EOFError):
            self.assertIsNone(await read_prompt())
