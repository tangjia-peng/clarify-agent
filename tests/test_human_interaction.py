"""【新增】原 Agent 的人机交互回归：模拟模型流，不请求外部模型或向量接口。"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from src.agent import Agent, SessionExit, TaskCancelled
from src.index import ask_in_terminal, main
from test_core import FakeStream, chunk, part


def tool_stream(*calls):
    """每个调用用 (工具名, 参数, 调用 ID) 表示，经过真实流式解析器。"""
    return FakeStream([chunk(calls=[
        part(index, call_id, name, json.dumps(args, ensure_ascii=False))
        for index, (name, args, call_id) in enumerate(calls)
    ])])


def mcp_client(*names):
    return NS(
        init=AsyncMock(), close=AsyncMock(),
        get_tools=lambda: [{"name": name, "inputSchema": {"type": "object"}} for name in names],
        call_tool=AsyncMock(return_value={"content": [{"type": "text", "text": "完成"}]}),
    )


class InteractionAgentTests(unittest.IsolatedAsyncioTestCase):
    async def start_agent(self, streams, callback=None, mcp=None, system_prompt=""):
        requests = []
        responses = iter(streams)

        async def create(**kwargs):
            requests.append(deepcopy(kwargs))
            return next(responses)

        api = NS(chat=NS(completions=NS(create=AsyncMock(side_effect=create))), close=AsyncMock())
        mcp = mcp if mcp is not None else mcp_client("read_file", "write_file")
        with patch("src.chat_openai.required_env", return_value="test"), patch(
            "src.chat_openai.AsyncOpenAI", return_value=api,
        ):
            agent = Agent("test", [mcp], system_prompt, ask_user_callback=callback)
            await agent.init()
        self.addAsyncCleanup(agent.close)
        return NS(agent=agent, api=api, requests=requests, mcp=mcp)

    async def test_local_ask_tool_is_registered_alongside_mcp_tools(self):
        state = await self.start_agent(
            [FakeStream([chunk("直接回答")])], AsyncMock(), system_prompt="保留用户指定的系统规则。",
        )
        await state.agent.invoke("信息已经完整")
        definitions = state.requests[0]["tools"]
        names = [tool["function"]["name"] for tool in definitions]
        self.assertCountEqual(names, ["read_file", "write_file", "ask_user"])
        ask = next(tool["function"] for tool in definitions if tool["function"]["name"] == "ask_user")
        self.assertIn("question", ask["parameters"]["required"])
        self.assertEqual(ask["parameters"]["properties"]["question"]["type"], "string")
        system = "\n".join(m["content"] for m in state.requests[0]["messages"] if m["role"] == "system")
        self.assertIn("保留用户指定的系统规则。", system)
        self.assertIn("ask_user", system)

    async def test_model_can_ask_twice_and_redecide_before_using_mcp(self):
        callback = AsyncMock(side_effect=["原材料延迟", "周五交付"])
        state = await self.start_agent([
            tool_stream(("ask_user", {"question": "延期的原因是什么？"}, "ask-reason")),
            tool_stream(("ask_user", {"question": "新的交付时间是什么？"}, "ask-date")),
            tool_stream(("write_file", {"path": "邮件.md", "content": "原材料延迟，周五交付。"}, "write-new")),
            FakeStream([chunk("邮件已保存。")]),
        ], callback)
        self.assertEqual(await state.agent.invoke("给客户写邮件并保存"), "邮件已保存。")
        self.assertEqual([c.args[0] for c in callback.await_args_list], ["延期的原因是什么？", "新的交付时间是什么？"])
        first_result = state.requests[1]["messages"][-1]
        self.assertEqual(first_result["tool_call_id"], "ask-reason")
        self.assertEqual(json.loads(first_result["content"]), {"answer": "原材料延迟"})
        second_result = state.requests[2]["messages"][-1]
        self.assertEqual(second_result["tool_call_id"], "ask-date")
        self.assertEqual(json.loads(second_result["content"]), {"answer": "周五交付"})
        state.mcp.call_tool.assert_awaited_once_with(
            "write_file", {"path": "邮件.md", "content": "原材料延迟，周五交付。"},
        )
        self.assertEqual(len(state.requests), 4)
        state.api.close.assert_not_awaited()
        state.mcp.close.assert_not_awaited()

    async def test_pending_user_reply_pauses_model_requests_and_mcp_execution(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def callback(question):
            entered.set()
            await release.wait()
            return "保存到确认后的文件.md"

        state = await self.start_agent([
            tool_stream(("ask_user", {"question": "保存在哪里？"}, "waiting")),
            tool_stream(("write_file", {"path": "确认后的文件.md", "content": "已确认"}, "write")),
            FakeStream([chunk("完成")]),
        ], callback)
        task = asyncio.create_task(state.agent.invoke("生成并保存"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            self.assertFalse(task.done())
            self.assertEqual(len(state.requests), 1)
            state.mcp.call_tool.assert_not_awaited()
            release.set()
            self.assertEqual(await asyncio.wait_for(task, timeout=2), "完成")
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        state.mcp.call_tool.assert_awaited_once_with(
            "write_file", {"path": "确认后的文件.md", "content": "已确认"},
        )
        self.assertEqual(
            json.loads(state.requests[1]["messages"][-1]["content"]),
            {"answer": "保存到确认后的文件.md"},
        )

    async def test_entire_mixed_batch_is_deferred_even_when_other_tool_comes_first(self):
        callback = AsyncMock(return_value="另存为 new.md")
        state = await self.start_agent([
            tool_stream(
                ("write_file", {"path": "old.md", "content": "未经确认的内容"}, "old-write"),
                ("ask_user", {"question": "保存在哪里？"}, "ask-path"),
                ("read_file", {"path": "unneeded.md"}, "old-read"),
            ),
            tool_stream(("write_file", {"path": "new.md", "content": "确认后的内容"}, "new-write")),
            FakeStream([chunk("完成")]),
        ], callback)
        self.assertEqual(await state.agent.invoke("修改并保存"), "完成")
        state.mcp.call_tool.assert_awaited_once_with("write_file", {"path": "new.md", "content": "确认后的内容"})
        results = {m["tool_call_id"]: json.loads(m["content"]) for m in state.requests[1]["messages"] if m["role"] == "tool"}
        self.assertCountEqual(results, ["old-write", "ask-path", "old-read"])
        self.assertIn("deferred", results["old-write"])
        self.assertIn("deferred", results["old-read"])
        self.assertEqual(results["ask-path"], {"answer": "另存为 new.md"})

    async def test_mcp_can_run_without_any_question(self):
        callback = AsyncMock()
        state = await self.start_agent([
            tool_stream(("read_file", {"path": "资料.md"}, "read")),
            FakeStream([chunk("回答完成")]),
        ], callback)
        self.assertEqual(await state.agent.invoke("读取指定文件"), "回答完成")
        callback.assert_not_awaited()
        state.mcp.call_tool.assert_awaited_once_with("read_file", {"path": "资料.md"})

    async def test_no_callback_does_not_advertise_unusable_local_tool(self):
        state = await self.start_agent([FakeStream([chunk("正常回答")])])
        await state.agent.invoke("你好")
        names = [tool["function"]["name"] for tool in state.requests[0]["tools"]]
        self.assertNotIn("ask_user", names)
        with self.assertRaises(RuntimeError):
            await state.agent.ask_user("不会被显示的问题")

    async def test_mcp_named_ask_user_is_not_intercepted_when_local_callback_is_absent(self):
        mcp = mcp_client("ask_user", "write_file")
        state = await self.start_agent([
            tool_stream(
                ("ask_user", {"question": "由 MCP 服务处理的问题"}, "remote-ask"),
                ("write_file", {"path": "结果.md", "content": "普通 MCP 操作"}, "remote-write"),
            ),
            FakeStream([chunk("两个 MCP 工具均已执行")]),
        ], callback=None, mcp=mcp)
        self.assertEqual(await state.agent.invoke("使用服务端工具"), "两个 MCP 工具均已执行")
        self.assertEqual(
            [(c.args[0], c.args[1]) for c in mcp.call_tool.await_args_list],
            [
                ("ask_user", {"question": "由 MCP 服务处理的问题"}),
                ("write_file", {"path": "结果.md", "content": "普通 MCP 操作"}),
            ],
        )
        results = {
            m["tool_call_id"]: json.loads(m["content"])
            for m in state.requests[1]["messages"] if m["role"] == "tool"
        }
        self.assertCountEqual(results, ["remote-ask", "remote-write"])
        for result in results.values():
            self.assertNotIn("deferred", result)
            self.assertFalse(result.get("isError", False))

    async def test_name_collision_fails_initialization_and_closes_mcp(self):
        mcp = mcp_client("ask_user")
        with patch("src.agent.ChatOpenAI") as factory:
            agent = Agent("test", [mcp], ask_user_callback=AsyncMock())
            with self.assertRaises(ValueError):
                await agent.init()
        factory.assert_not_called()
        mcp.close.assert_awaited_once()
        self.assertIsNone(agent.llm)

    async def test_invalid_question_is_returned_as_tool_error_then_can_be_retried(self):
        callback = AsyncMock(return_value="用户补充信息")
        state = await self.start_agent([
            tool_stream(("ask_user", {"question": "   "}, "invalid")),
            tool_stream(("ask_user", {"question": "请补充具体信息。"}, "valid")),
            FakeStream([chunk("已获得信息")]),
        ], callback)
        self.assertEqual(await state.agent.invoke("帮助处理任务"), "已获得信息")
        callback.assert_awaited_once_with("请补充具体信息。")
        failed = json.loads(state.requests[1]["messages"][-1]["content"])
        self.assertTrue(failed["isError"])
        self.assertEqual(json.loads(state.requests[2]["messages"][-1]["content"]), {"answer": "用户补充信息"})

    async def test_callback_error_is_a_tool_error_not_a_session_shutdown(self):
        callback = AsyncMock(side_effect=[RuntimeError("界面暂时不可用"), "重新取得答复"])
        state = await self.start_agent([
            tool_stream(("ask_user", {"question": "补充信息？"}, "first")),
            tool_stream(("ask_user", {"question": "现在可以补充吗？"}, "retry")),
            FakeStream([chunk("继续完成")]),
        ], callback)
        self.assertEqual(await state.agent.invoke("需要帮助"), "继续完成")
        self.assertTrue(json.loads(state.requests[1]["messages"][-1]["content"])["isError"])
        state.api.close.assert_not_awaited()

    async def test_cancel_and_exit_roll_back_current_turn_without_running_pending_mcp(self):
        for exception_type in (TaskCancelled, SessionExit):
            with self.subTest(exception=exception_type.__name__):
                callback = AsyncMock(side_effect=exception_type())
                state = await self.start_agent([
                    FakeStream([chunk("之前成功的回答")]),
                    tool_stream(
                        ("write_file", {"path": "不应写入.md"}, "pending-write"),
                        ("ask_user", {"question": "是否继续？"}, "cancel-ask"),
                    ),
                    FakeStream([chunk("新任务完成")]),
                ], callback)
                await state.agent.invoke("之前的问题")
                history = deepcopy(state.agent.llm.messages)
                with self.assertRaises(exception_type):
                    await state.agent.invoke("需要取消的任务")
                self.assertEqual(state.agent.llm.messages, history)
                state.mcp.call_tool.assert_not_awaited()
                state.api.close.assert_not_awaited()
                self.assertEqual(await state.agent.invoke("新的任务"), "新任务完成")


class TerminalInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_collects_free_text_and_retries_blank_input(self):
        with patch("builtins.input", side_effect=["", "   ", "另存为新故事.md"]) as read:
            self.assertEqual(await ask_in_terminal("保存到哪里？"), "另存为新故事.md")
        self.assertEqual(read.call_count, 3)

    async def test_terminal_cancel_only_cancels_current_task(self):
        with patch("builtins.input", return_value="/cancel"):
            with self.assertRaises(TaskCancelled):
                await ask_in_terminal("是否继续？")

    async def test_terminal_exit_and_eof_end_session(self):
        for value in ("/exit", "/quit", "EXIT", "quit", EOFError()):
            with self.subTest(value=str(value)), patch("builtins.input", side_effect=[value]):
                with self.assertRaises(SessionExit):
                    await ask_in_terminal("请回答。")

    async def test_main_passes_terminal_callback_and_can_continue_after_cancel(self):
        agent = NS(
            init=AsyncMock(), close=AsyncMock(),
            invoke=AsyncMock(side_effect=[TaskCancelled(), "下个任务完成"]),
        )
        with patch("src.index.Agent", return_value=agent) as factory, patch(
            "src.index.create_retriever", AsyncMock(return_value=NS(retrieve=AsyncMock(return_value=[])))), patch(
            "src.index.create_mcp_clients", return_value=[],
        ), patch("builtins.input", side_effect=["下一个任务", "/exit"]):
            self.assertEqual(await main("取消这个任务"), "下个任务完成")
        self.assertIs(factory.call_args.kwargs["ask_user_callback"], ask_in_terminal)
        self.assertEqual([c.args[0] for c in agent.invoke.await_args_list], ["取消这个任务", "下一个任务"])
        agent.close.assert_awaited_once()

    async def test_main_does_not_request_another_task_after_exit_inside_ask(self):
        agent = NS(init=AsyncMock(), close=AsyncMock(), invoke=AsyncMock(side_effect=SessionExit()))
        with patch("src.index.Agent", return_value=agent), patch(
            "src.index.create_retriever", AsyncMock(return_value=NS(retrieve=AsyncMock(return_value=[])))), patch(
            "src.index.create_mcp_clients", return_value=[],
        ), patch("builtins.input") as read:
            self.assertEqual(await main("执行中退出"), "")
        read.assert_not_called()
        agent.close.assert_awaited_once()
