"""整篇资料去重回归：不调用模型、向量接口或外部工具。"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from src.context_documents import (
    join_context_documents,
    merge_contexts,
    split_context_documents,
)

# 单独运行本文件时也不读取真实 .env，裁判导入设置的评测种子不影响其他测试。
with patch("dotenv.load_dotenv", return_value=False), patch.dict(os.environ):
    from src.agent import Agent
    from src.chat_openai import ChatResponse
    from src.index import retrieve_context
    from eval.judge import load_context


class ContextDocumentTests(unittest.TestCase):
    def test_overlapping_retrievals_deduplicate_whole_documents_in_order(self):
        a = "# 景点A\n门票：免费\n开放：9:00-17:00\n"
        b = "# 景点B\n门票：免费\n开放：9:00-17:00\n"
        c = "# 景点C\n门票：免费\n"
        first = join_context_documents([a, b])
        second = join_context_documents([b, c, a])

        merged = merge_contexts(first, second)

        self.assertEqual(split_context_documents(merged), [a, b, c])
        self.assertEqual(merged.count("门票：免费"), 3)
        self.assertEqual(merged.count("开放：9:00-17:00"), 2)

    def test_internal_repetition_whitespace_and_nested_markers_are_preserved(self):
        nested = join_context_documents(["原文里的框架示例😀\n"])
        document = (
            "\n\t# 景点A\r\n门票：免费\r\n\r\n"
            "# 正文中的另一个标题\n门票：免费\n"
            "<!-- original-body:start -->\n"
            + nested
            + "\n<!-- original-body:end -->\n\t\n"
        )

        merged = merge_contexts(join_context_documents([document]), "")

        self.assertEqual(split_context_documents(merged), [document])
        self.assertEqual(merged.count("门票：免费"), 2)
        self.assertIn(document, merged)

    def test_same_source_with_different_full_text_is_not_deleted(self):
        source = "原帖：https://example.test/note/1\n"
        old = source + "景点A\n门票：免费\n"
        updated = source + "景点A\n门票：免费\n景点B\n"

        merged = merge_contexts(
            join_context_documents([old]), join_context_documents([updated])
        )

        self.assertEqual(split_context_documents(merged), [old, updated])

    def test_text_with_different_trailing_whitespace_is_not_treated_as_identical(self):
        first = "景点A\n门票：免费\n"
        second = first + "\n"
        merged = merge_contexts(join_context_documents([first, second, first]))
        self.assertEqual(split_context_documents(merged), [first, second])

    def test_legacy_context_is_kept_whole_with_repeated_lines(self):
        legacy = "# 景点A\n门票：免费\n\n# 景点B\n门票：免费\n"

        merged = merge_contexts(legacy, legacy)

        self.assertEqual(split_context_documents(merged), [legacy])
        self.assertEqual(merged.count("门票：免费"), 2)

    def test_incomplete_or_mixed_framing_does_not_discard_any_text(self):
        framed = join_context_documents(["景点A\n门票：免费\n", "景点B\n门票：免费\n"])
        for damaged in (framed[:-1], "历史说明\n" + framed, framed + "\n额外尾部"):
            with self.subTest(damaged=damaged):
                self.assertEqual(split_context_documents(damaged), [damaged])
                self.assertEqual(split_context_documents(merge_contexts(damaged)), [damaged])

    def test_empty_retrieval_stays_empty(self):
        self.assertEqual(join_context_documents([]), "")
        self.assertEqual(split_context_documents(""), [])
        self.assertEqual(merge_contexts("", ""), "")

    def test_judge_and_agent_use_the_same_complete_documents(self):
        a = "# 景点A\n门票：免费\n门票：免费\n"
        b = "# 景点B\n门票：免费\n"
        c = "# 景点C\n开放：9:00-17:00\n"
        first = join_context_documents([a, b])
        second = join_context_documents([b, c])
        with TemporaryDirectory(prefix="context-merge-test-") as directory:
            runs = Path(directory)
            (runs / "T01-context.txt").write_text(first, encoding="utf-8", newline="\n")
            (runs / "T01-B2-context2.txt").write_text(second, encoding="utf-8", newline="\n")

            judged = load_context("T01-B2", runs)

            self.assertEqual(judged, Agent._merge_contexts(first, second))
            self.assertEqual(split_context_documents(judged), [a, b, c])
            for mode in ("A", "B"):
                with self.subTest(mode=mode):
                    self.assertEqual(split_context_documents(load_context("T01-" + mode, runs)), [a, b])
            self.assertEqual(load_context("T02-B2", runs), "（无资料）")


class ContextDocumentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieval_keeps_document_boundaries_without_extra_requests(self):
        documents = ["景点A\n门票：免费\n", "景点B\n门票：免费\n"]
        retriever = NS(retrieve=AsyncMock(return_value=documents))

        context = await retrieve_context("安排两天行程", retriever=retriever)

        retriever.retrieve.assert_awaited_once_with("安排两天行程")
        self.assertEqual(split_context_documents(context), documents)

    async def test_b2_final_renderer_receives_all_repeated_facts(self):
        a = "# 景点A\n门票：免费\n开放：9:00-17:00\n"
        b = "# 景点B\n门票：免费\n开放：9:00-17:00\n"
        first = join_context_documents([a, b])
        second = join_context_documents([b])
        retrieve_again = AsyncMock(return_value=second)
        messages = [{"role": "user", "content": "规划一日游"},
                    {"role": "assistant", "content": "初稿"}]

        async def chat(prompt=None, context=None, *, allow_tools=True):
            messages.append({"role": "user", "content": prompt})
            messages.append({"role": "assistant", "content": "最终攻略"})
            return ChatResponse("最终攻略", [])

        llm = NS(messages=messages, chat=AsyncMock(side_effect=chat))
        agent = Agent("test", [])
        agent.llm = llm

        final, merged, rendered = await agent.render_final(
            "规划一日游", [], "初稿", first, retrieve_again
        )

        self.assertEqual(final, "最终攻略")
        self.assertTrue(rendered)
        self.assertEqual(split_context_documents(merged), [a, b])
        retrieve_again.assert_awaited_once_with("规划一日游")
        llm.chat.assert_awaited_once()
        self.assertFalse(llm.chat.await_args.kwargs["allow_tools"])
        sent_user_content = llm.chat.await_args.args[0]
        self.assertIn(merged, sent_user_content)
        self.assertEqual(sent_user_content.count("门票：免费"), 2)
        self.assertEqual(sent_user_content.count("开放：9:00-17:00"), 2)


if __name__ == "__main__":
    unittest.main()
