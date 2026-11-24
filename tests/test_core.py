"""不调用付费接口、不联网的回归测试。"""

import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.agent import Agent
from src.chat_openai import ChatOpenAI, ChatResponse, FunctionCall, ToolCall
from src.embedding_retriever import EmbeddingRetriever
from src.vector_store import VectorStore


class VectorTests(unittest.TestCase):
    def test_ranking_and_limits(self):
        store = VectorStore()
        store.add_embedding([0, 1], "第二名")
        store.add_embedding([1, 0], "第一名")
        store.add_embedding([-1, 0], "第三名")
        self.assertEqual(store.search([1, 0], 2), ["第一名", "第二名"])
        self.assertEqual(store.search([1, 0], 0), [])
        self.assertEqual(store.cosine_similarity([0, 0], [1, 0]), 0)
        with self.assertRaises(ValueError):
            store.search([1])


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for chunk in self.chunks:
            yield chunk

    async def close(self):
        self.closed = True


def part(index, id=None, name=None, arguments=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=arguments))


def chunk(content=None, calls=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=calls))])


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_interleaved_stream_and_empty_usage_chunk(self):
        stream = FakeStream([
            chunk("查询中", [part(1, "b", "weather", '{"city":'), part(0, "a", "weather", '{"city":')]),
            NS(choices=[]),
            chunk(calls=[part(0, arguments='"北京"}'), part(1, arguments='"上海"}')]),
        ])
        client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=stream))), close=AsyncMock())
        with patch("src.chat_openai.required_env", return_value="test"), patch("src.chat_openai.AsyncOpenAI", return_value=client):
            chat = ChatOpenAI("test-model")
        response = await chat.chat("天气")
        self.assertEqual(response.content, "查询中")
        self.assertEqual([call.id for call in response.tool_calls], ["a", "b"])
        self.assertEqual(json.loads(response.tool_calls[1].function.arguments), {"city": "上海"})
        self.assertTrue(stream.closed)
        self.assertNotIn("tools", client.chat.completions.create.call_args.kwargs)
        chat.append_tool_result("a", "晴")
        self.assertEqual(chat.messages[-1]["tool_call_id"], "a")
        await chat.close()

    async def test_final_message_omits_empty_tool_calls(self):
        stream = FakeStream([chunk("你好")])
        client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=stream))))
        with patch("src.chat_openai.required_env", return_value="test"), patch("src.chat_openai.AsyncOpenAI", return_value=client):
            chat = ChatOpenAI("test-model")
        await chat.chat("你好")
        self.assertNotIn("tool_calls", chat.messages[-1])


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_tool_rounds_and_reverse_cleanup(self):
        events = []

        class Client:
            def __init__(self, name):
                self.name = name

            async def init(self):
                events.append("init-" + self.name)

            def get_tools(self):
                return [{"name": self.name, "inputSchema": {"type": "object"}}]

            async def call_tool(self, name, params):
                events.append(name)
                return {"content": [{"type": "text", "text": "完成"}]}

            async def close(self):
                events.append("close-" + self.name)

        class LLM:
            def __init__(self, *args):
                self.messages = []  # 【新增】模拟持久的会话消息。
                self.responses = iter([
                    ChatResponse("", [ToolCall("1", FunctionCall("fetch", "{}"))]),
                    ChatResponse("", [ToolCall("2", FunctionCall("write", '{"path":"a.md"}'))]),
                    ChatResponse("完成", []),
                ])
                self.results = []
                self.closed = False

            async def chat(self, prompt=None, context=None):
                return next(self.responses)

            def append_tool_result(self, id, output):
                self.results.append((id, output))

            async def close(self):
                self.closed = True

        with patch("src.agent.ChatOpenAI", LLM):
            agent = Agent("test", [Client("fetch"), Client("write")])
            await agent.init()
            llm = agent.llm
            self.assertEqual(await agent.invoke("完成任务"), "完成")
            self.assertFalse(llm.closed)  # 【新增】一轮结束仍保持连接。
            await agent.close()
            await agent.close()  # 重复关闭也安全
        self.assertEqual(events, ["init-fetch", "init-write", "fetch", "write", "close-write", "close-fetch"])
        self.assertEqual(len(llm.results), 2)
        self.assertTrue(llm.closed)

    # 【修改】普通单轮错误回滚消息，保留连接，最终由会话入口关闭。
    async def test_failure_rolls_back_and_allows_next_turn(self):
        client = NS(init=AsyncMock(), get_tools=lambda: [], close=AsyncMock())
        messages = [{"role": "user", "content": "之前的问题"}]

        async def fail(prompt, context=None):
            messages.extend([
                {"role": "user", "content": prompt},
                {"role": "assistant", "tool_calls": [{"id": "unfinished"}]},
            ])
            raise RuntimeError("断网")

        llm = NS(messages=messages, chat=AsyncMock(side_effect=fail), close=AsyncMock())
        with patch("src.agent.ChatOpenAI", return_value=llm):
            agent = Agent("test", [client])
            await agent.init()
            with self.assertRaisesRegex(RuntimeError, "断网"):
                await agent.invoke("你好")
            self.assertEqual(messages, [{"role": "user", "content": "之前的问题"}])
            client.close.assert_not_awaited()
            llm.chat.side_effect = None
            llm.chat.return_value = ChatResponse("恢复了", [])
            self.assertEqual(await agent.invoke("继续"), "恢复了")
            await agent.close()
        client.close.assert_awaited_once()
        llm.close.assert_awaited_once()


class EmbeddingTests(unittest.IsolatedAsyncioTestCase):
    # 【新增】错误路径使用本地 MockTransport，验证诊断信息和密钥脱敏。
    async def embed_error(self, response, document, api_key="unit-test-secret"):
        real_client = httpx.AsyncClient
        with patch.dict("os.environ", {
            "EMBEDDING_BASE_URL": "https://example.invalid/v1", "EMBEDDING_KEY": api_key,
        }), patch(
            "src.embedding_retriever.httpx.AsyncClient",
            side_effect=lambda **kw: real_client(
                transport=httpx.MockTransport(lambda request: response), **kw,
            ),
        ):
            retriever = EmbeddingRetriever("unit-test-embedding-model")
            with self.assertRaises(httpx.HTTPStatusError) as caught:
                await retriever.embed(document)
        self.assertIs(caught.exception.response, response)
        self.assertEqual(caught.exception.request.url.path, "/v1/embeddings")
        return caught.exception

    async def test_http_error_exposes_json_details_but_redacts_secret(self):
        api_key = "unit-test-private-api-key"
        document = "PRIVATE_INPUT_MUST_NOT_APPEAR:" + "x" * 23
        response = httpx.Response(
            400,
            json={"code": 20012, "message": f"Embedding model is unsupported; key={api_key}"},
            headers={"x-siliconcloud-trace-id": "trace-400-json"},
        )
        error = await self.embed_error(response, document, api_key)
        message = str(error)
        for detail in (
            "400", "unit-test-embedding-model", str(len(document)),
            "20012", "Embedding model is unsupported", "trace-400-json", "[REDACTED]",
        ):
            self.assertIn(detail, message)
        self.assertNotIn(api_key, message)
        self.assertNotIn(document, message)
        self.assertNotIn("Authorization", message)

    async def test_http_error_exposes_nested_error_code_and_message(self):
        document = "NESTED_ERROR_PRIVATE_INPUT"
        response = httpx.Response(400, json={
            "error": {"code": "invalid_embedding_model", "message": "Requested model does not support embeddings"},
        })
        message = str(await self.embed_error(response, document))
        self.assertIn("invalid_embedding_model", message)
        self.assertIn("Requested model does not support embeddings", message)
        self.assertIn("unit-test-embedding-model", message)
        self.assertNotIn(document, message)

    async def test_non_json_http_error_has_bounded_fallback_and_trace(self):
        api_key = "unit-test-fallback-secret"
        document = "NON_JSON_PRIVATE_INPUT_MUST_NOT_APPEAR"
        body = (f"upstream rejected request; key={api_key}\n" * 100) + "THIS_TAIL_SHOULD_BE_TRUNCATED"
        response = httpx.Response(
            400, text=body,
            headers={"x-siliconcloud-trace-id": "trace-400-text"},
        )
        message = str(await self.embed_error(response, document, api_key))
        self.assertIn("upstream rejected request", message)
        self.assertIn("trace-400-text", message)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn(api_key, message)
        self.assertNotIn(document, message)
        self.assertNotIn("THIS_TAIL_SHOULD_BE_TRUNCATED", message)
        self.assertLess(len(message), 1200)

    async def test_request_and_retrieval(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}]})

        real_client = httpx.AsyncClient
        with patch.dict("os.environ", {"EMBEDDING_BASE_URL": "https://example.invalid/v1", "EMBEDDING_KEY": "test"}), patch(
            "src.embedding_retriever.httpx.AsyncClient",
            side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
        ):
            retriever = EmbeddingRetriever("test-model")
            await retriever.embed_document("完整文档")
            self.assertEqual(await retriever.retrieve("问题"), ["完整文档"])
        self.assertEqual(requests[0]["input"], "完整文档")
        self.assertEqual(requests[1]["input"], "问题")


if __name__ == "__main__":
    unittest.main()
