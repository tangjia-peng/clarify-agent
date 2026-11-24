"""B2 沿原历史生成终版及评测快照回归；所有模型与检索接口均模拟。"""

from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

with patch("dotenv.load_dotenv", return_value=False), patch.dict(os.environ):
    from src.agent import Agent
    from src.chat_openai import ChatOpenAI
    from src.context_documents import join_context_documents, split_context_documents
    from eval import judge, run_ab

from test_core import FakeStream, chunk, part


DRAFT = "本轮完整攻略草稿：此文本不应进入终版请求。"
TOOLS = [{"name": "fetch", "inputSchema": {"type": "object"}}]


def history():
    return [
        {"role": "system", "content": "原会话系统提示词"},
        {"role": "user", "content": "北京三日游，带70岁父母，不去长城。"},
        {"role": "assistant", "content": "此前已经完成的方案，后续可以继续修改。"},
        {"role": "user", "content": "改成五天，其他不变。"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "ask-1", "type": "function", "function": {
                "name": "ask_user", "arguments": '{"question":"住宿区域？"}'}}]},
        {"role": "tool", "tool_call_id": "ask-1", "content": '{"answer":"东城"}'},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "fetch-1", "type": "function", "function": {
                "name": "fetch", "arguments": '{"url":"https://example.test"}'}}]},
        {"role": "tool", "tool_call_id": "fetch-1", "content": "已取得的工具结果"},
        {"role": "assistant", "content": DRAFT},
    ]


class BrokenStream(FakeStream):
    async def iterate(self):
        yield chunk("未完成的终版")
        raise RuntimeError("模拟流中断")


class B2ContinuationTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, streams):
        requests = []
        remaining = iter(streams)

        async def create(**kwargs):
            requests.append(deepcopy(kwargs))
            return next(remaining)

        api = NS(chat=NS(completions=NS(create=AsyncMock(side_effect=create))), close=AsyncMock())
        with patch("src.chat_openai.required_env", return_value="test"), patch(
            "src.chat_openai.AsyncOpenAI", return_value=api,
        ):
            llm = ChatOpenAI("test-model", tools=TOOLS)
        llm.messages.extend(history())
        agent = Agent("test-model", [])
        agent.llm = llm
        return agent, llm, requests

    async def finalize(self, agent):
        first = join_context_documents(["景点A\n门票：免费\n"])
        second = join_context_documents(["景点B\n门票：免费\n"])
        return await agent.render_final(
            "改成五天，其他不变。", [("住宿区域？", "东城")], DRAFT,
            first, AsyncMock(return_value=second),
        )

    async def test_final_request_preserves_history_excludes_draft_and_disables_tools_once(self):
        agent, llm, requests = self.make_agent([
            FakeStream([chunk("最终攻略")]), FakeStream([chunk("下一轮回答")]),
        ])
        original_list = llm.messages
        before = deepcopy(llm.messages)

        final, merged, rendered = await self.finalize(agent)

        self.assertEqual((final, rendered), ("最终攻略", True))
        sent = requests[0]["messages"]
        self.assertEqual(sent[:-1], before[:-1])
        self.assertIn(merged, sent[-1]["content"])
        self.assertEqual(split_context_documents(merged), ["景点A\n门票：免费\n", "景点B\n门票：免费\n"])
        self.assertNotIn(DRAFT, json.dumps(sent, ensure_ascii=False))
        self.assertNotIn("tools", requests[0])
        self.assertIs(llm.messages, original_list)
        self.assertEqual(llm.messages[:-2], before[:-1])
        self.assertEqual(llm.messages[-1], {"role": "assistant", "content": "最终攻略"})
        self.assertEqual(sum(m.get("content") == "最终攻略" for m in llm.messages), 1)

        await llm.chat("继续下一轮")

        self.assertIn("tools", requests[1])
        self.assertEqual(llm.tools, TOOLS)

    async def test_interrupted_or_empty_final_restores_draft_and_can_continue(self):
        for stream in (BrokenStream([]), FakeStream([])):
            with self.subTest(stream=type(stream).__name__):
                agent, llm, requests = self.make_agent([stream, FakeStream([chunk("可以继续")])])
                before = deepcopy(llm.messages)
                original_list = llm.messages
                if isinstance(stream, BrokenStream):
                    with self.assertRaisesRegex(RuntimeError, "模拟流中断"):
                        await self.finalize(agent)
                else:
                    final, _, rendered = await self.finalize(agent)
                    self.assertEqual((final, rendered), (DRAFT, False))
                self.assertEqual(llm.messages, before)
                self.assertIs(llm.messages, original_list)
                self.assertTrue(stream.closed)
                await llm.chat("继续")
                self.assertIn("tools", requests[-1])
                self.assertEqual(llm.messages[-1]["content"], "可以继续")

    async def test_unexpected_final_tool_call_is_not_executed_or_left_in_history(self):
        stream = FakeStream([chunk(calls=[part(0, "unexpected", "fetch", "{}")])])
        agent, llm, _ = self.make_agent([stream])
        before = deepcopy(llm.messages)
        agent._execute_tool = AsyncMock()

        with self.assertRaises((RuntimeError, ValueError)):
            await self.finalize(agent)

        agent._execute_tool.assert_not_awaited()
        self.assertEqual(llm.messages, before)

    async def test_empty_retrieval_keeps_original_history_without_extra_generation(self):
        agent, llm, requests = self.make_agent([])
        before = deepcopy(llm.messages)
        result = await agent.render_final("问题", [], DRAFT, "", AsyncMock(return_value=""))
        self.assertEqual((result[0], result[2]), (DRAFT, False))
        self.assertEqual(llm.messages, before)
        self.assertEqual(requests, [])

    async def test_record_replay_restores_full_history_and_requires_a_snapshot(self):
        agent, llm, requests = self.make_agent([FakeStream([chunk("回放终版")])])
        saved = history()
        before = deepcopy(saved)
        llm.messages.clear()
        original_list = llm.messages

        final = await agent.finalize_from_record(
            "改成五天，其他不变。", [("住宿区域？", "东城")], DRAFT,
            "首轮资料", AsyncMock(return_value="二次资料"), messages=saved,
        )

        self.assertEqual(final, "回放终版")
        self.assertEqual(requests[0]["messages"][:-1], before[:-1])
        self.assertEqual(saved, before)
        self.assertIs(llm.messages, original_list)
        with self.assertRaises((ValueError, RuntimeError)):
            await agent.finalize_from_record("问题", [], DRAFT, "资料", AsyncMock())


class EvalContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_b_snapshot_roundtrips_into_b2_without_restarting_mcp(self):
        task = {"题目": "北京一日游\n带70岁父母，不去长城。", "隐藏": []}
        saved_messages = [{"role": "user", "content": task["题目"]},
                          {"role": "assistant", "content": DRAFT}]
        b = NS(llm=NS(messages=deepcopy(saved_messages)), init=AsyncMock(),
               invoke=AsyncMock(return_value=DRAFT))

        async def close_b():
            b.llm = None

        b.close = AsyncMock(side_effect=close_b)
        b2 = NS(init=AsyncMock(), close=AsyncMock(), last_draft=DRAFT)

        async def finalize(prompt, qa, draft, first_context, callback, *, messages):
            self.assertEqual(messages, saved_messages)
            self.assertEqual((prompt, draft, first_context), (task["题目"], DRAFT, "首轮资料"))
            await callback("澄清后的检索问题")
            return "B2终版"

        b2.finalize_from_record = AsyncMock(side_effect=finalize)
        with TemporaryDirectory(prefix="eval-history-test-") as directory:
            runs = Path(directory)
            with patch.object(run_ab, "RUNS_DIR", runs), patch.object(
                run_ab, "Agent", side_effect=[b, b2],
            ) as factory, patch.object(run_ab, "create_mcp_clients", return_value=[]) as clients, patch.object(
                run_ab, "retrieve_context", AsyncMock(side_effect=["首轮资料", "二次资料"]),
            ):
                await run_ab.run_one("T01", "B", task, object())
                record = json.loads((runs / "T01-B-messages.json").read_text(encoding="utf-8"))
                self.assertEqual(record["messages"], saved_messages)
                self.assertEqual(record["question"], task["题目"])
                self.assertIn(record["run_id"], (runs / "T01-B.md").read_text(encoding="utf-8"))
                await run_ab.run_b2_borrowed("T01", task, object())
                self.assertEqual(factory.call_args_list[1].args[1], [])
                clients.assert_called_once()
            self.assertIn("B2终版", (runs / "T01-B2.md").read_text(encoding="utf-8"))
            self.assertEqual((runs / "T01-B2-context2.txt").read_text(encoding="utf-8"), "二次资料")

    async def test_missing_or_mismatched_b_history_cannot_silently_replay(self):
        with TemporaryDirectory(prefix="eval-history-missing-") as directory:
            runs = Path(directory)
            (runs / "T01-B.md").write_text("旧的B结果", encoding="utf-8")
            with patch.object(run_ab, "RUNS_DIR", runs), patch.object(run_ab, "Agent") as factory:
                with self.assertRaises((FileNotFoundError, ValueError)):
                    await run_ab.run_b2_borrowed("T01", {"题目": "新问题"}, object())
                old_task = {"题目": "旧问题"}
                run_ab.write_run_md(runs / "T01-B.md", "T01", "B", old_task, [], DRAFT, run_id="old-run")
                record = {
                    "format_version": run_ab.EVAL_FORMAT_VERSION, "run_id": "old-run",
                    "task_id": "T01", "question": "旧问题", "qa": [], "draft": DRAFT,
                    "first_context": "旧资料", "messages": [
                        {"role": "user", "content": "旧问题"},
                        {"role": "assistant", "content": DRAFT},
                    ],
                }
                (runs / "T01-B-messages.json").write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaises(ValueError):
                    await run_ab.run_b2_borrowed("T01", {"题目": "新问题"}, object())
                factory.assert_not_called()

    async def test_judge_accepts_only_current_successful_matching_b2_pairs(self):
        with TemporaryDirectory(prefix="eval-history-judge-") as directory:
            runs = Path(directory)
            b_path = runs / "T01-B.md"
            b2_path = runs / "T01-B2.md"
            task = {"题目": "北京一日游"}
            b2_path.write_text("模式：B2\n题目：北京一日游\n【最终攻略】\n旧终版", encoding="utf-8")
            self.assertTrue(judge.result_problem(b2_path, judge.parse_run(b2_path)))

            run_ab.write_run_md(b_path, "T01", "B", task, [], DRAFT, run_id="same-run")
            run_ab.write_run_md(b2_path, "T01", "B2", task, [], "新终版", run_id="same-run")
            self.assertEqual(judge.result_problem(b2_path, judge.parse_run(b2_path)), "")

            run_ab.write_run_md(b_path, "T01", "B", task, [], DRAFT, run_id="different-run")
            self.assertTrue(judge.result_problem(b2_path, judge.parse_run(b2_path)))
            run_ab.write_run_md(b_path, "T01", "B", task, [], DRAFT, run_id="same-run")
            b_path.with_suffix(".error.txt").write_text("本次B失败", encoding="utf-8")
            self.assertTrue(judge.result_problem(b2_path, judge.parse_run(b2_path)))

    async def test_invalid_old_score_is_preserved_in_dry_run_and_archived_otherwise(self):
        with TemporaryDirectory(prefix="eval-history-score-") as directory:
            home = Path(directory)
            runs = home / "runs"
            outputs = home / "scores"
            runs.mkdir()
            outputs.mkdir()
            (runs / "T01-B2.md").write_text(
                "模式：B2\n题目：北京一日游\n【最终攻略】\n旧流程终版", encoding="utf-8",
            )
            old_score = outputs / "T01-B2.txt"
            original_bytes = "追问轮数：2\r\n旧评分内容原样保留。\r\n".encode("utf-8")
            old_score.write_bytes(original_bytes)
            with patch.object(judge, "EVAL_HOME", home), patch.object(judge, "RUNS_DIR", runs), patch.object(
                judge, "OUT_DIR", outputs,
            ), patch.object(judge, "load_checklist", return_value={}), patch.object(
                judge, "load_hidden", return_value={},
            ), patch("openai.OpenAI") as client:
                judge.main("T01", dry=True)
                self.assertEqual(old_score.read_bytes(), original_bytes)
                self.assertFalse((outputs / "历史结果").exists())

                judge.main("T01", dry=False)

                self.assertFalse(old_score.exists())
                archived = list((outputs / "历史结果").glob("T01-B2-*.txt.archived"))
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0].read_bytes(), original_bytes)
                self.assertEqual(list(outputs.rglob("*.txt")), [])
                client.assert_not_called()

    async def test_mode_order_runs_b_first_and_failed_b_cannot_reuse_old_result(self):
        with TemporaryDirectory(prefix="eval-history-order-") as directory:
            home = Path(directory)
            (home / "评估任务集模板.csv").touch()
            runs = home / "runs"
            task = {"题目": "北京一日游", "隐藏": []}
            for fail_b in (False, True):
                with self.subTest(fail_b=fail_b):
                    order = []

                    async def run_one(task_id, mode, task, retriever):
                        order.append(mode)
                        if fail_b and mode == "B":
                            raise RuntimeError("本次B失败")
                        return runs / f"{task_id}-{mode}.md"

                    with patch.object(run_ab, "EVAL_HOME", home), patch.object(run_ab, "RUNS_DIR", runs), patch.object(
                        run_ab, "load_tasks", return_value={"T01": task},
                    ), patch.object(run_ab, "create_retriever", AsyncMock()), patch.object(
                        run_ab, "run_one", AsyncMock(side_effect=run_one),
                    ):
                        await run_ab.main("T01", "B2,B")
                    self.assertEqual(order, ["B"] if fail_b else ["B", "B2"])


if __name__ == "__main__":
    unittest.main()
