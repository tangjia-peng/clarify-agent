"""网页事件回传回归：使用真实流式解析器和模拟模型，不访问外部 API。"""

from copy import deepcopy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from src.agent import Agent, TaskCancelled
from src.chat_openai import ChatOpenAI
from test_core import FakeStream, chunk, part


def tool_stream(*calls):
    return FakeStream([chunk(calls=[
        part(index, call_id, name, json.dumps(args, ensure_ascii=False))
        for index, (name, args, call_id) in enumerate(calls)
    ])])


def mcp_client():
    return NS(
        init=AsyncMock(), close=AsyncMock(),
        get_tools=lambda: [{"name": "write_file", "description": "写入文件", "inputSchema": {"type": "object"}}],
        call_tool=AsyncMock(return_value={"content": [{"type": "text", "text": "已写入"}]}),
    )


class WebChatEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_start_and_nonempty_tokens_are_emitted_in_order(self):
        events = []

        async def emit(event):
            events.append(deepcopy(event))

        stream = FakeStream([chunk("你好"), NS(choices=[]), chunk("，世界"), chunk()])
        api = NS(chat=NS(completions=NS(create=AsyncMock(return_value=stream))), close=AsyncMock())
        with patch("src.chat_openai.required_env", return_value="test"), patch(
            "src.chat_openai.AsyncOpenAI", return_value=api,
        ):
            chat = ChatOpenAI("test-model", event_callback=emit)
        self.addAsyncCleanup(chat.close)
        response = await chat.chat("打招呼")
        self.assertEqual(response.content, "你好，世界")
        self.assertEqual(events, [
            {"type": "response_start"},
            {"type": "delta", "content": "你好"},
            {"type": "delta", "content": "，世界"},
        ])
        self.assertTrue(stream.closed)
        self.assertEqual(chat.messages[-1], {"role": "assistant", "content": "你好，世界"})

    async def test_token_callback_cancellation_still_closes_stream(self):
        async def emit(event):
            if event["type"] == "delta":
                raise TaskCancelled()

        stream = FakeStream([chunk("部分内容"), chunk("不应继续读取")])
        api = NS(chat=NS(completions=NS(create=AsyncMock(return_value=stream))), close=AsyncMock())
        with patch("src.chat_openai.required_env", return_value="test"), patch(
            "src.chat_openai.AsyncOpenAI", return_value=api,
        ):
            chat = ChatOpenAI("test-model", event_callback=emit)
        self.addAsyncCleanup(chat.close)
        with self.assertRaises(TaskCancelled):
            await chat.chat("可以取消")
        self.assertTrue(stream.closed)
        self.assertFalse(any(message["role"] == "assistant" for message in chat.messages))


class WebAgentEventTests(unittest.IsolatedAsyncioTestCase):
    async def start_agent(self, streams, event_callback, ask_user_callback=None, mcp=None):
        requests = []
        responses = iter(streams)

        async def create(**kwargs):
            requests.append(deepcopy(kwargs))
            return next(responses)

        api = NS(chat=NS(completions=NS(create=AsyncMock(side_effect=create))), close=AsyncMock())
        client = mcp if mcp is not None else mcp_client()
        with patch("src.chat_openai.required_env", return_value="test"), patch(
            "src.chat_openai.AsyncOpenAI", return_value=api,
        ):
            agent = Agent(
                "test-model", [client], ask_user_callback=ask_user_callback,
                event_callback=event_callback,
            )
            await agent.init()
        self.addAsyncCleanup(agent.close)
        return NS(agent=agent, api=api, mcp=client, requests=requests)

    async def test_tools_and_executed_tool_events_match_model_history(self):
        events = []
        state = None

        async def emit(event):
            events.append(deepcopy(event))
            if event["type"] == "tool_start":
                state.mcp.call_tool.assert_not_awaited()
            elif event["type"] == "tool_result":
                history_result = state.agent.llm.messages[-1]
                self.assertEqual(history_result["role"], "tool")
                self.assertEqual(history_result["tool_call_id"], event["id"])
                self.assertEqual(json.loads(history_result["content"]), event["result"])

        state = await self.start_agent([
            tool_stream(("write_file", {"path": "草稿.md", "content": "已确认内容"}, "write-1")),
            FakeStream([chunk("完成")]),
        ], emit)
        self.assertEqual(await state.agent.invoke("保存草稿"), "完成")
        tools_event = next(event for event in events if event["type"] == "tools")
        self.assertEqual(tools_event["tools"], [{"name": "write_file", "description": "写入文件"}])
        tool_events = [event for event in events if event["type"] in {"tool_start", "tool_result"}]
        self.assertEqual([event["type"] for event in tool_events], ["tool_start", "tool_result"])
        self.assertEqual(tool_events[0]["id"], "write-1")
        self.assertEqual(tool_events[0]["arguments"], {"path": "草稿.md", "content": "已确认内容"})
        state.mcp.call_tool.assert_awaited_once_with("write_file", tool_events[0]["arguments"])

    async def test_deferred_call_emits_no_tool_start_and_only_new_decision_executes(self):
        events = []

        async def emit(event):
            events.append(deepcopy(event))

        state = await self.start_agent([
            tool_stream(
                ("write_file", {"path": "旧决定.md", "content": "未经确认"}, "old-write"),
                ("ask_user", {"question": "请确认保存位置。"}, "ask-path"),
            ),
            tool_stream(("write_file", {"path": "新决定.md", "content": "用户确认"}, "new-write")),
            FakeStream([chunk("已保存")]),
        ], emit, AsyncMock(return_value="新决定.md"))
        self.assertEqual(await state.agent.invoke("生成并保存内容"), "已保存")
        old_events = [event for event in events if event.get("id") == "old-write"]
        self.assertEqual(len(old_events), 1)
        self.assertEqual(old_events[0]["type"], "tool_deferred")
        self.assertTrue(old_events[0]["result"]["deferred"])
        state.mcp.call_tool.assert_awaited_once_with("write_file", {"path": "新决定.md", "content": "用户确认"})
        second_request_history = state.requests[1]["messages"]
        self.assertEqual([m["tool_call_id"] for m in second_request_history if m["role"] == "tool"], ["old-write", "ask-path"])

    async def test_tool_start_cancellation_propagates_before_mcp_and_rolls_back_turn(self):
        async def emit(event):
            if event["type"] == "tool_start":
                raise TaskCancelled()

        state = await self.start_agent([
            FakeStream([chunk("之前的回答")]),
            tool_stream(("write_file", {"path": "不应创建.md", "content": "不应写入"}, "cancelled-write")),
        ], emit)
        await state.agent.invoke("之前的问题")
        previous_history = deepcopy(state.agent.llm.messages)
        with self.assertRaises(TaskCancelled):
            await state.agent.invoke("需要取消的任务")
        self.assertEqual(state.agent.llm.messages, previous_history)
        state.mcp.call_tool.assert_not_awaited()
        state.api.close.assert_not_awaited()

    async def test_stream_event_cancellation_rolls_back_and_can_accept_next_turn(self):
        cancel_enabled = False

        async def emit(event):
            if cancel_enabled and event["type"] == "delta":
                raise TaskCancelled()

        cancelled_stream = FakeStream([chunk("部分回答"), chunk("不会读取")])
        state = await self.start_agent([
            FakeStream([chunk("成功回答")]),
            cancelled_stream,
            FakeStream([chunk("下一轮回答")]),
        ], emit)
        await state.agent.invoke("成功的问题")
        previous_history = deepcopy(state.agent.llm.messages)
        cancel_enabled = True
        with self.assertRaises(TaskCancelled):
            await state.agent.invoke("取消的问题")
        self.assertTrue(cancelled_stream.closed)
        self.assertEqual(state.agent.llm.messages, previous_history)
        cancel_enabled = False
        self.assertEqual(await state.agent.invoke("下一个问题"), "下一轮回答")


if __name__ == "__main__":
    unittest.main()
