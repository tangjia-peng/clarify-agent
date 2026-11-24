"""【新增】网页会话回归：不调用外部模型或向量服务。"""

import asyncio
from contextlib import suppress
from copy import deepcopy
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.agent import Agent
from src.chat_openai import ChatResponse, FunctionCall, ToolCall
from src.mcp_client import MCPClient
from src.web_app import BrowserSession, MAX_MESSAGE_CHARS, create_app


class Socket:
    def __init__(self):
        self.events = asyncio.Queue()

    async def send_json(self, event):
        await self.events.put(deepcopy(event))


class FakeAgent:
    instances = []

    def __init__(self, model, clients, *, ask_user_callback, event_callback, finalize_context=None):
        self.ask = ask_user_callback
        self.emit = event_callback
        self.finalize_context = finalize_context  # B2 接入后产品入口会传入，桩需接收但不触发
        self.llm = None
        self.history = []
        self.owner = None
        self.closed = False
        self.__class__.instances.append(self)

    async def init(self):
        self.owner = asyncio.current_task()
        self.llm = object()
        await self.emit({"type": "tools", "tools": [{"name": "ask_user"}]})

    async def invoke(self, content, context=None):
        if asyncio.current_task() is not self.owner:
            raise RuntimeError("Agent changed tasks")
        self.history.append(content)
        await self.emit({"type": "response_start"})
        if content == "询问":
            answer = await self.ask("你希望保存到哪里？")
            answer2 = await self.ask("文件使用什么标题？")
            result = f"已按 {answer}、{answer2} 继续处理"
        elif content == "持续执行":
            while True:
                await asyncio.sleep(0.01)
                await self.emit({"type": "delta", "content": "处理中"})
        elif content == "错误":
            raise RuntimeError("upstream echoed " + os.getenv("OPENAI_API_KEY", ""))
        else:
            result = f"回答{len(self.history)}：{content}"
        await self.emit({"type": "delta", "content": result})
        return result

    async def close(self):
        if self.owner is not None and asyncio.current_task() is not self.owner:
            raise RuntimeError("MCP cleanup changed tasks")
        self.closed = True
        self.llm = None


class WebSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeAgent.instances = []
        self.socket = Socket()
        self.session = BrowserSession(self.socket)
        self.retriever = NS(retrieve=AsyncMock(return_value=["测试资料"]))
        self.patches = [
            patch("src.web_app.Agent", FakeAgent),
            patch("src.web_app.create_retriever", AsyncMock(return_value=self.retriever)),
            patch("src.web_app.create_mcp_clients", return_value=[]),
        ]
        self.active_patches = [p.start() for p in self.patches]
        self.worker = asyncio.create_task(self.session.run())
        self.addAsyncCleanup(self.shutdown)

    async def shutdown(self):
        self.session.disconnected = True
        self.worker.cancel()
        with suppress(asyncio.CancelledError):
            await self.worker
        for p in reversed(self.patches):
            p.stop()

    async def event(self, kind, state=None):
        async with asyncio.timeout(5):
            while True:
                event = await self.socket.events.get()
                if event["type"] == kind and (state is None or event.get("state") == state):
                    return event

    async def send_task(self, content):
        await self.session.accept_command({"type": "message", "content": content})

    async def finish_turn(self):
        event = await self.event("done")
        await self.event("status", "idle")
        return event

    async def test_two_turns_reuse_agent_and_knowledge_and_close_in_owner_task(self):
        await self.send_task("第一问")
        self.assertEqual((await self.finish_turn())["content"], "回答1：第一问")
        await self.send_task("第二问")
        self.assertEqual((await self.finish_turn())["content"], "回答2：第二问")
        self.active_patches[1].assert_awaited_once()
        self.assertEqual(len(FakeAgent.instances), 1)
        self.assertEqual(FakeAgent.instances[0].history, ["第一问", "第二问"])
        self.assertIs(FakeAgent.instances[0].owner, self.worker)

    async def test_questions_pause_and_free_text_replies_resume_original_task(self):
        await self.send_task("询问")
        question = await self.event("question")
        self.assertFalse(self.worker.done())
        self.assertIsNotNone(self.session.pending_answer)
        await self.send_task("不能插队")
        self.assertEqual((await self.event("error"))["code"], "busy")
        await self.session.accept_command({"type": "answer", "request_id": "old", "content": "错位答复"})
        self.assertEqual((await self.event("error"))["code"], "stale_question")
        self.assertFalse(self.session.pending_answer.done())
        await self.session.accept_command({"type": "answer", "request_id": question["request_id"], "content": "项目目录"})
        self.assertEqual((await self.event("answer_accepted"))["content"], "项目目录")
        second = await self.event("question")
        self.assertNotEqual(second["request_id"], question["request_id"])
        await self.session.accept_command({"type": "answer", "request_id": second["request_id"], "content": "新故事"})
        self.assertEqual((await self.finish_turn())["content"], "已按 项目目录、新故事 继续处理")
        self.assertEqual(FakeAgent.instances[0].history, ["询问"])

    async def test_cancel_pending_question_then_new_task_and_reset(self):
        await self.send_task("询问")
        await self.event("question")
        first = FakeAgent.instances[0]
        await self.session.accept_command({"type": "reset"})
        self.assertEqual((await self.event("error"))["code"], "busy")
        await self.session.accept_command({"type": "cancel"})
        await self.event("cancelled")
        await self.event("status", "idle")
        await self.send_task("取消后的问题")
        await self.finish_turn()
        self.assertFalse(first.closed)
        await self.session.accept_command({"type": "reset"})
        await self.event("reset")
        await self.event("status", "idle")
        self.assertTrue(first.closed)
        await self.send_task("全新对话")
        self.assertEqual((await self.finish_turn())["content"], "回答1：全新对话")
        self.assertEqual(len(FakeAgent.instances), 2)

    async def test_cancel_running_stream_at_next_event(self):
        await self.send_task("持续执行")
        await self.event("delta")
        await self.session.accept_command({"type": "cancel"})
        await self.event("status", "cancelling")
        await self.event("cancelled")
        await self.event("status", "idle")
        self.assertFalse(self.worker.done())

    async def test_cancel_before_worker_starts_is_not_lost(self):
        await self.send_task("不应执行")
        await self.session.accept_command({"type": "cancel"})
        await self.event("cancelled")
        await self.event("status", "idle")
        self.assertEqual(FakeAgent.instances, [])
        await self.send_task("可以执行")
        await self.finish_turn()

    async def test_tool_result_is_delivered_even_when_cancel_is_pending(self):
        self.session.cancel_requested.set()
        from src.agent import TaskCancelled
        with self.assertRaises(TaskCancelled):
            await self.session.on_event({"type": "tool_result", "id": "a", "name": "write_file", "result": {"saved": True}})
        result = await self.event("tool_result")
        self.assertTrue(result["result"]["saved"])

    async def test_question_send_failure_does_not_leave_pending_reply(self):
        from src.agent import SessionExit
        with patch.object(self.session, "status", AsyncMock(side_effect=SessionExit())):
            with self.assertRaises(SessionExit):
                await self.session.ask_user("不会成功发送的问题")
        self.assertIsNone(self.session.pending_answer)
        self.assertIsNone(self.session.question_id)

    async def test_upstream_error_is_redacted_and_conversation_can_continue(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sensitive-test-key"}):
            await self.send_task("错误")
            error = await self.event("error")
            self.assertNotIn("sensitive-test-key", error["message"])
            self.assertIn("[REDACTED]", error["message"])
            await self.event("status", "idle")
        await self.send_task("错误后的问题")
        await self.finish_turn()


class WebEndpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        directory = Path(self.directory.name)
        (directory / "index.html").write_text("<!doctype html><title>Agent UI</title>", encoding="utf-8")
        (directory / "app.js").write_text("console.log('local');", encoding="utf-8")
        self.client = TestClient(create_app(directory), base_url="http://127.0.0.1")
        self.addCleanup(self.client.close)

    def connect(self, origin="http://127.0.0.1"):
        return self.client.websocket_connect("ws://127.0.0.1/ws", headers={"origin": origin})

    def test_static_pages_and_ready_do_not_call_external_services(self):
        with patch("src.web_app.create_retriever") as retrieve, patch("src.web_app.Agent") as agent:
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/static/app.js").status_code, 200)
            self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
            self.assertEqual(self.client.get("/.env").status_code, 404)
            self.assertEqual(self.client.get("/static/%2e%2e/.env").status_code, 404)
            with self.connect() as socket:
                self.assertEqual(socket.receive_json()["type"], "ready")
                self.assertEqual(socket.receive_json()["state"], "idle")
            retrieve.assert_not_called()
            agent.assert_not_called()

    def test_outside_or_missing_origin_is_rejected(self):
        for origin in ("https://example.com", "http://localhost", "null", ""):
            with self.subTest(origin=origin), self.assertRaises(WebSocketDisconnect):
                with self.connect(origin):
                    pass

    def test_invalid_json_and_large_frames_are_recoverable(self):
        with self.connect() as socket:
            socket.receive_json()
            socket.receive_json()
            for raw in ("invalid-json", "[]", "x" * (MAX_MESSAGE_CHARS + 1)):
                socket.send_text(raw)
                self.assertEqual(socket.receive_json()["code"], "invalid_message")
            socket.send_json({"type": "cancel"})
            self.assertEqual(socket.receive_json()["state"], "idle")

    def test_non_string_message_types_are_rejected_without_disconnect_or_api_calls(self):
        invalid = [{"type": value} for value in ([], {}, None, 42, 1.5, True, False)] + [{}]
        with patch("src.web_app.create_retriever") as retrieve, patch("src.web_app.Agent") as agent, patch(
            "src.web_app.create_mcp_clients",
        ) as clients:
            with self.connect() as socket:
                self.assertEqual(socket.receive_json()["type"], "ready")
                self.assertEqual(socket.receive_json()["state"], "idle")
                for command in invalid:
                    with self.subTest(command=command):
                        socket.send_json(command)
                        error = socket.receive_json()
                        self.assertEqual(error["type"], "error")
                        self.assertEqual(error["code"], "invalid_message")
                        socket.send_json({"type": "cancel"})
                        self.assertEqual(socket.receive_json()["state"], "idle")
            retrieve.assert_not_called()
            agent.assert_not_called()
            clients.assert_not_called()

    def test_disconnect_while_waiting_for_answer_closes_real_mcp(self):
        fixture = str(Path(__file__).with_name("mcp_fixture.py"))
        mcp = MCPClient("browser-question", sys.executable, [fixture])
        tasks = {}
        original_init, original_close = mcp.init, mcp.close

        async def init():
            tasks["init"] = asyncio.current_task()
            await original_init()

        async def close():
            tasks["close"] = asyncio.current_task()
            await original_close()

        mcp.init, mcp.close = init, close

        class LLM:
            def __init__(self, *args, **kwargs):
                self.messages = []

            async def chat(self, prompt=None, context=None):
                if prompt:
                    self.messages.append({"role": "user", "content": prompt})
                return ChatResponse("", [ToolCall("ask-live", FunctionCall("ask_user", '{"question":"保存在哪里？"}'))])

            def append_tool_result(self, call_id, output):
                self.messages.append({"role": "tool", "content": output, "tool_call_id": call_id})

            async def close(self):
                pass

        retriever = NS(retrieve=AsyncMock(return_value=[]))
        with patch("src.agent.ChatOpenAI", LLM), patch(
            "src.web_app.create_retriever", AsyncMock(return_value=retriever),
        ), patch("src.web_app.create_mcp_clients", return_value=[mcp]):
            with self.connect() as socket:
                self.assertEqual(socket.receive_json()["type"], "ready")
                socket.receive_json()
                socket.send_json({"type": "message", "content": "准备一个需要确认的文件任务"})
                for _ in range(20):
                    event = socket.receive_json()
                    if event["type"] == "question":
                        break
                else:
                    self.fail("没有收到模型询问")
                self.assertIsNotNone(mcp.session)
                # 不作答就关闭 WebSocket，模拟关闭浏览器页签。
            self.assertIsNone(mcp.session)
            self.assertIs(tasks["init"], tasks["close"])


class RealMCPWebLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cleans_up_real_stdio_scope_in_same_worker_task(self):
        """真实本地子进程验证 AnyIO 作用域不会跨请求/Task 关闭。"""
        fixture = str(Path(__file__).with_name("mcp_fixture.py"))
        mcp = MCPClient("test", sys.executable, [fixture])
        socket = Socket()
        session = BrowserSession(socket)

        class LLM:
            def __init__(self, *args, **kwargs):
                self.messages = []
                self.count = 0

            async def chat(self, prompt=None, context=None):
                self.count += 1
                if prompt:
                    self.messages.append({"role": "user", "content": prompt})
                if self.count == 1:
                    return ChatResponse("", [ToolCall("echo-1", FunctionCall("echo", '{"text":"网页测试"}'))])
                return ChatResponse("完成", [])

            def append_tool_result(self, call_id, output):
                self.messages.append({"role": "tool", "content": output, "tool_call_id": call_id})

            async def close(self):
                pass

        retriever = NS(retrieve=AsyncMock(return_value=[]))
        with patch("src.web_app.Agent", Agent), patch("src.agent.ChatOpenAI", LLM), patch(
            "src.web_app.create_retriever", AsyncMock(return_value=retriever),
        ), patch("src.web_app.create_mcp_clients", return_value=[mcp]):
            worker = asyncio.create_task(session.run())
            try:
                await session.accept_command({"type": "message", "content": "调用 echo"})
                async with asyncio.timeout(10):
                    while True:
                        event = await socket.events.get()
                        if event["type"] == "done":
                            self.assertEqual(event["content"], "完成")
                        if event["type"] == "status" and event["state"] == "idle":
                            break
                self.assertIsNotNone(mcp.session)
            finally:
                session.disconnected = True
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
        self.assertIsNone(mcp.session)


if __name__ == "__main__":
    unittest.main()
