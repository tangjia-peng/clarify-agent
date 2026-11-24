# -*- coding: utf-8 -*-
"""A/B 测试自动跑题脚本：读评估任务集 CSV，逐题跑模式 A / 模式 B，结果存到评估工作区。

用法（在项目根目录执行）：
    uv run --cache-dir .uv-cache python eval/run_ab.py                 # 跑全部题的 A、B
    uv run --cache-dir .uv-cache python eval/run_ab.py --tasks T01     # 只跑 T01
    uv run --cache-dir .uv-cache python eval/run_ab.py --tasks T01,T02 --modes B
    uv run --cache-dir .uv-cache python eval/run_ab.py --tasks T01 --modes B,B2 # 跑 B 再跑 B2（B2 复用 B）

原理：
    模式 A ：Agent 不传 ask_user_callback —— 提问工具根本不注册，Agent 只能直接生成攻略
    模式 B ：Agent 传"模拟用户 AI"回调 —— 追问由大模型按 CSV「藏起来的信息」扮演真实用户
            作答：设定内如实答；设定外合理即兴答（不与设定矛盾）；开放兜底问题才倒出剩余设定。
            最多认真回答 15 个问题，第 16 个起直接收口（防无限追问）。失败退回关键词匹配。
    模式 B2：复用 B 保存的完整 messages，不重新追问；用「首问+B 的追问问答」二次检索，
            去掉本轮完整草稿、加入补充资料，沿用其余历史，本次不提供工具并输出终版。
            追问记录与 B 逐字相同；B/B2 比较的是“二次检索后重新生成终版”的整体流程。
            B2 依赖当前版本的 B 历史快照；指定 B2 时会确保先运行 B。
输出：
    <评估工作区>/对话记录/T01-A.md、T01-B.md、T01-B2.md（追问轮数、追问记录、最终攻略）
    <评估工作区>/对话记录/T01-context.txt（首轮检索资料，给裁判用）
    <评估工作区>/对话记录/T01-A-context.txt、T01-B-context.txt（各模式实际首轮资料）
    <评估工作区>/对话记录/T01-B-messages.json（B 的完整历史与配对信息，B2 必需）
    <评估工作区>/对话记录/T01-B2-context2.txt（B2 二次检索资料，给裁判核终版）
    <评估工作区>/对话记录/T01-B2-draft.md（B2 被替换的草稿，仅调试，裁判不读）

扮演用户的模型默认与攻略 Agent 同一个（读 .env）；想换别的模型可在 .env 加：
    SIM_USER_MODEL=xxx  以及对应 SIM_USER_API_KEY / SIM_USER_BASE_URL
（用不同模型扮演用户，避免"同一个模型自问自答"过于默契）
"""
import argparse
import asyncio
import csv
from copy import deepcopy
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # 让 eval/ 里的脚本能 import src

from src.agent import Agent            # noqa: E402（导入时自动加载项目 .env）
from src.config import required_env    # noqa: E402
from src.index import create_mcp_clients, create_retriever, retrieve_context  # noqa: E402

# 【修改】默认工作区跟随项目位置，支持文件夹改名及路径中的空格。
EVAL_HOME = Path(os.getenv("EVAL_HOME") or PROJECT_ROOT / "result")
RUNS_DIR = EVAL_HOME / "对话记录"
EVAL_FORMAT_VERSION = "continue-history-v1"

# 评估可复现：固定随机种子（模拟用户 AI / 主模型 / 裁判都读这个）。
# 产品正常运行不设这个环境变量，行为完全不变；只在 eval 脚本里设默认值。
os.environ.setdefault("EVAL_SEED", "20261117")

# 模拟用户最多认真回答 15 个问题；第 16 个起直接收口，让 Agent 停止追问、直接出攻略
MAX_SIM_QUESTIONS = 15
STOP_ANSWER = "不用再问了，我已经说的这些信息足够了，请你直接给我出一份攻略吧。"

# 问题主题 -> 藏起来的信息里该找哪条（模式 B 自动答题用）
TOPIC_PATTERNS = [
    (r"预算|花费|多少钱|费用|价位|人均", r"预算|人均|元|钱"),
    (r"几天|多少天|玩多久|几天几夜|时长|待多久", r"天|日|晚"),
    (r"几个人|几位|同行|人数|一个人|和谁|带谁|几人", r"人|位|独自|带|岁|女朋友|团建"),
    (r"目的地|去哪|想去哪|哪个城市|哪座城|去哪里|什么地方|城市", r"目的地|江浙沪|杭州|成都|苏州|北京|西安|桂林|大理|厦门|长沙|重庆|哈尔滨|东京"),
    (r"什么时候|几月|月份|出行时间|什么季节|哪天|周几", r"五一|冬天|雨季|周末|周六|周日|时间|看雪"),
    (r"吃|辣|素|忌口|口|餐饮|餐厅|口味|食物", r"辣|素|忌口|吃|清淡|美食|小吃|火锅|清汤"),
    (r"小孩|孩子|老人|父母|爸妈|父母|体力|累|强度|怕冷", r"小孩|孩子|老人|爸妈|父母|岁|体力|累|怕冷|怕高"),
    (r"喜欢|偏好|兴趣|想看|想玩|想体验|想安排|爱好", r"喜欢|偏好|想|自然|人文|历史|摄影|美食|夜生活|海边|雪|骑行|刺激|团队"),
    (r"怎么去|交通|包车|车|高铁|飞机|集合|到达|返程", r"包车|高铁|车|集合|返程|骑行|直达"),
    (r"住|住宿|酒店|青旅|民宿|标间|睡", r"住|青旅|标间|供暖|民宿"),
]
import re  # noqa: E402
TOPIC_PATTERNS = [(re.compile(q), re.compile(a)) for q, a in TOPIC_PATTERNS]


def pick_answer(question: str, hidden_items: list[str], used: list[bool]) -> str:
    """按问题主题，从还没给过的隐藏信息里挑一条作答。"""
    if not hidden_items:
        return "没有特别要求，你自己决定就好。"
    # 先按主题匹配
    for q_pat, a_pat in TOPIC_PATTERNS:
        if q_pat.search(question):
            for i, item in enumerate(hidden_items):
                if not used[i] and a_pat.search(item):
                    used[i] = True
                    return item
    # 主题对不上：给下一条没给过的
    for i, item in enumerate(hidden_items):
        if not used[i]:
            used[i] = True
            return item
    return "没有了，我能想到的就这些，请你直接帮我安排吧。"


def load_tasks(csv_path: Path) -> dict[str, dict]:
    tasks = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tid = row["题号"].strip().upper()
            hidden = row["藏起来的信息（Agent追问时你照着这些回答；别给Agent看）"].strip()
            items = [s.strip() for s in hidden.split(";") if s.strip() and s.strip() != "无"]
            tasks[tid] = {
                "题目": row["用户原话（只给Agent看的题目）"].strip(),
                "隐藏": items,
            }
    return tasks


# ============ 模式 B 的"模拟用户 AI"（大模型扮演用户回答追问） ============

SIM_USER_PROMPT = """你在扮演一位正向AI旅游助手咨询的真实用户，助手刚向你提了一个问题。
请像真人一样回答：简短自然，一两句话就好。

【你的设定（出题人定的真实信息，硬性，必须遵守）】
{profile}

【之前的问答（你已说过的不要重复、不要矛盾）】
{history}

【助手的问题】
{question}

回答规则：
1. 设定里有对应信息的，如实、直接地回答。
2. 只答被问到的内容——不要主动把设定里其他还没被问到的信息倒出来。
3. 问题涉及的点不在设定里（比如问坐高铁还是飞机、喜欢几点出门），就自由合理地即兴回答；
   但不得与设定矛盾，也不得新增会改变行程规划的硬性条件（预算/天数/人数/忌口/同行人/健康等一律以设定为准）。
4. 如果问题是"还有什么要补充/其他要求"这类开放式收口问题：把设定中还没说过的信息都告诉对方；
   如果都说过了，就答"没有了，你帮我安排就行"。
5. 设定为空时，你就是一位没有特殊要求的普通用户，自由合理回答即可。

只输出你的回答本身，不要解释、不要加引号。"""


def sim_user_answer(task: dict, history: list[tuple[str, str]], question: str) -> str:
    """调用大模型扮演用户回答追问：设定内如实答，设定外即兴答。"""
    from openai import OpenAI
    client = OpenAI(
        api_key=os.getenv("SIM_USER_API_KEY") or required_env("OPENAI_API_KEY"),
        base_url=os.getenv("SIM_USER_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None,
    )
    model = os.getenv("SIM_USER_MODEL") or os.getenv("OPENAI_MODEL") or "deepseek-v4-pro"
    hidden = task["隐藏"]
    profile = "\n".join(f"- {h}" for h in hidden) if hidden else "（无——你没有任何特殊要求）"
    history_text = ("\n".join(f"问：{q}\n答：{a}" for q, a in history)
                    if history else "（还没有问答，这是第一个问题）")
    prompt = SIM_USER_PROMPT.format(profile=profile, history=history_text, question=question)
    resp = client.chat.completions.create(
        model=model, temperature=0, seed=int(os.getenv("EVAL_SEED", "20261117")),
        messages=[{"role": "user", "content": prompt}],
    )
    answer = (resp.choices[0].message.content or "").strip()
    return answer or "你看着安排就行。"


def parse_run_md(path: Path) -> dict:
    """解析 run_ab.py 产出的 md，取出追问问答与最终攻略（供 B2 复用 B 的轨迹）。"""
    import re as _re
    text = path.read_text(encoding="utf-8")
    qa = [
        (m.group(2).strip(), m.group(3).strip())
        for m in _re.finditer(
            r"问题(\d+)：([\s\S]*?)\n回答\1：([\s\S]*?)(?=\n问题\d+：|\n【最终攻略】|\Z)",
            text,
        )
    ]
    m = _re.search(r"【最终攻略】\n?([\s\S]*)", text)
    draft = m.group(1).strip() if m else ""
    question = _re.search(
        r"^题目：([\s\S]*?)\n【追问记录】(?:无)?(?:\n|$)", text, _re.MULTILINE,
    )
    def field(name: str) -> str:
        match = _re.search(r"^" + _re.escape(name) + r"：(.+)$", text, _re.MULTILINE)
        return match.group(1).strip() if match else ""
    return {"问答": qa, "攻略": draft, "题目": question.group(1) if question else "",
            "流程版本": field("流程版本"), "运行标识": field("运行标识")}


def _md_text(value: str) -> str:
    """仅在比对 MD 时兼容跨平台换行；实际模型历史使用快照原文。"""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def write_run_md(out: Path, task_id: str, mode: str, task: dict,
                 qa: list[tuple[str, str]], final: str, *, run_id: str = "") -> None:
    """把一次运行结果写成统一格式的 md（裁判依赖这个固定格式）。"""
    lines = [
        f"题号：{task_id}",
        f"模式：{mode}",
        f"流程版本：{EVAL_FORMAT_VERSION}",
        f"运行标识：{run_id}",
        f"追问轮数：{len(qa)}",
        f"题目：{task['题目']}",
        "【追问记录】" if qa else "【追问记录】无",
    ]
    for i, (q, a) in enumerate(qa, 1):
        lines += [f"问题{i}：{q}", f"回答{i}：{a}"]
    lines += ["", "【最终攻略】", final or "（Agent 没有输出内容）"]
    out.write_text("\n".join(lines), encoding="utf-8")
    out.with_suffix(".error.txt").unlink(missing_ok=True)


async def run_b2_borrowed(task_id: str, task: dict, retriever) -> Path:
    """【修改】恢复 B 的完整历史，排除草稿并补资料，禁用工具后继续生成终版。"""
    b_file = RUNS_DIR / f"{task_id}-B.md"
    history_file = RUNS_DIR / f"{task_id}-B-messages.json"
    if (RUNS_DIR / f"{task_id}-B.error.txt").exists():
        raise RuntimeError("模式 B 最近运行失败，请先重跑成功，B2 不会复用旧结果。")
    if not b_file.exists() or not history_file.exists():
        raise FileNotFoundError(
            f"B2 需要当前版本 B 的完整历史：缺少 {b_file.name} 或 {history_file.name}。"
            "请先执行 --modes B 重跑；旧版只有问答的结果不能恢复完整 messages。"
        )
    b_result = parse_run_md(b_file)
    record = json.loads(history_file.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("B 的完整历史快照应为 JSON 对象，请先重跑模式 B。")
    if (
        record.get("format_version") != EVAL_FORMAT_VERSION
        or b_result["流程版本"] != EVAL_FORMAT_VERSION
        or record.get("task_id") != task_id or record.get("question") != task["题目"]
        or _md_text(b_result["题目"]) != _md_text(task["题目"])
        or not record.get("run_id") or record["run_id"] != b_result["运行标识"]
    ):
        raise ValueError("B 的题目、版本或运行标识与完整历史不匹配，请先重跑模式 B。")
    messages = record.get("messages")
    qa = record.get("qa")
    draft = record.get("draft")
    first_context = record.get("first_context")
    if (
        not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages)
        or not isinstance(qa, list) or not all(
            isinstance(pair, list) and len(pair) == 2 and all(isinstance(value, str) for value in pair)
            for pair in qa
        )
        or not isinstance(draft, str) or not isinstance(first_context, str)
    ):
        raise ValueError("B 的完整历史快照格式不正确，请先重跑模式 B。")
    qa = [tuple(pair) for pair in qa]
    if (
        [(_md_text(q), _md_text(a)) for q, a in qa] != b_result["问答"]
        or _md_text(draft) != b_result["攻略"]
        or messages[-1].get("role") != "assistant"
        or messages[-1].get("tool_calls")
        or (messages[-1].get("content") or "") != draft
    ):
        raise ValueError("B 的草稿或问答与完整历史不一致，请先重跑模式 B。")

    async def finalize_context(query: str) -> str:
        new_ctx = await retrieve_context(query, retriever=retriever)
        (RUNS_DIR / f"{task_id}-B2-context2.txt").write_text(new_ctx, encoding="utf-8")
        return new_ctx

    # B2 只续写终版；恢复历史后本次禁用工具，无需重新启动 MCP。
    agent = Agent(os.getenv("OPENAI_MODEL", "deepseek-v4-pro"), [])
    try:
        await agent.init()
        final = await agent.finalize_from_record(
            task["题目"], qa, draft, first_context, finalize_context, messages=messages,
        )
        if agent.last_draft:
            (RUNS_DIR / f"{task_id}-B2-draft.md").write_text(agent.last_draft, encoding="utf-8")
    finally:
        await agent.close()

    out = RUNS_DIR / f"{task_id}-B2.md"
    write_run_md(out, task_id, "B2", task, qa, final, run_id=record["run_id"])
    return out


async def run_one(task_id: str, mode: str, task: dict, retriever) -> Path:
    """跑一题的一种模式，返回结果文件路径。"""
    mode = mode.upper()
    # 【修改】B2 续接 B 保存的完整历史，不再自己跑追问。
    if mode == "B2":
        return await run_b2_borrowed(task_id, task, retriever)

    out = RUNS_DIR / f"{task_id}-{mode}.md"
    questions: list[tuple[str, str]] = []

    # 模式 B：回调先问"模拟用户 AI"（大模型扮演用户）；调用失败退回关键词匹配，不中断
    # 超过 MAX_SIM_QUESTIONS 个问题后直接收口（防止 Agent 无限追问）；模式 A：不传回调
    async def auto_answer(question: str) -> str:
        if len(questions) >= MAX_SIM_QUESTIONS:
            answer = STOP_ANSWER
            print(f"  已问 {len(questions)} 个问题，模拟用户收口：不再回答新问题", flush=True)
        else:
            try:
                answer = sim_user_answer(task, questions, question)
            except Exception as exc:
                print(f"  模拟用户AI调用失败（{exc}），本问退回关键词匹配", flush=True)
                answer = pick_answer(question, task["隐藏"], used)
        questions.append((question, answer))
        return answer

    used = [False] * len(task["隐藏"])
    clients = create_mcp_clients()
    options = {}
    if mode == "B":
        options["ask_user_callback"] = auto_answer

    agent = Agent(os.getenv("OPENAI_MODEL", "deepseek-v4-pro"), clients, **options)

    context = await retrieve_context(task["题目"], retriever=retriever)
    (RUNS_DIR / f"{task_id}-context.txt").write_text(context, encoding="utf-8")
    (RUNS_DIR / f"{task_id}-{mode}-context.txt").write_text(context, encoding="utf-8")

    try:
        await agent.init()
        final = await agent.invoke(task["题目"], context=context)
        if mode == "B":
            messages = deepcopy(agent.llm.messages)
    finally:
        await agent.close()

    run_id = uuid4().hex
    if mode == "B":
        # 【新增】原消息列表保留 user/assistant/tool 配对，不能仅靠 MD 问答重建。
        record = {"format_version": EVAL_FORMAT_VERSION, "run_id": run_id,
                  "task_id": task_id, "question": task["题目"], "qa": questions,
                  "draft": final, "first_context": context, "messages": messages}
        (RUNS_DIR / f"{task_id}-B-messages.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    write_run_md(out, task_id, mode, task, questions, final, run_id=run_id)
    return out


async def main(tasks_arg: str, modes_arg: str) -> None:
    csv_path = EVAL_HOME / "评估任务集模板.csv"
    if not csv_path.exists():
        print(f"找不到任务集：{csv_path}")
        sys.exit(1)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    all_tasks = load_tasks(csv_path)

    want = [t.strip().upper() for t in tasks_arg.split(",") if t.strip()] or list(all_tasks)
    modes = [m.strip().upper() for m in modes_arg.split(",") if m.strip()] or ["A", "B"]

    bad = [t for t in want if t not in all_tasks]
    if bad:
        print(f"任务集里没有这些题号：{bad}")
        sys.exit(1)
    bad_modes = [m for m in modes if m not in ("A", "B", "B2")]
    if bad_modes:
        print(f"没有这些模式（只支持 A、B、B2）：{bad_modes}")
        sys.exit(1)
    # 去重但保留顺序（防止 A,B,B 之类重复跑）
    modes = list(dict.fromkeys(modes))
    # 【修复】包括 --modes B2,B 在内，始终让本批 B 成功后再续接 B2。
    if "B2" in modes and ("B" not in modes or modes.index("B") > modes.index("B2")):
        if "B" in modes:
            modes.remove("B")
        modes.insert(modes.index("B2"), "B")
        print("B2 需要本批 B 的完整历史，已将模式 B 安排在 B2 前。")

    print(f"共 {len(want)} 题 × 模式 {'/'.join(modes)} = {len(want) * len(modes)} 次运行")
    print("正在构建知识库（只做一次）……")
    retriever = await create_retriever()

    for task_id in want:
        b_succeeded = False
        for mode in modes:
            print(f"\n>>> 运行 {task_id} 模式{mode} ……", flush=True)
            try:
                if mode == "B2" and not b_succeeded:
                    raise RuntimeError("本批模式 B 未成功，跳过 B2；不会读取上次运行的历史。")
                out = await run_one(task_id, mode, all_tasks[task_id], retriever)
                if mode == "B":
                    b_succeeded = True
                print(f"完成：{out}")
            except Exception as exc:
                err_file = RUNS_DIR / f"{task_id}-{mode}.error.txt"
                err_file.write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
                print(f"失败（已记录到 {err_file}）：{exc}")
    print("\n全部跑完。下一步：uv run --cache-dir .uv-cache python eval/judge.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="攻略 Agent A/B 跑题")
    parser.add_argument("--tasks", default="", help="逗号分隔的题号，如 T01,T02；默认全部")
    parser.add_argument("--modes", default="A,B", help="要跑的模式：A、B、B2，可逗号分隔；默认 A,B")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.tasks, args.modes))
    except KeyboardInterrupt:
        print("\n已中断。")
