# -*- coding: utf-8 -*-
"""自动裁判脚本：读 run_ab.py 的产出，调用模型按固定格式打分，输出直接对接自动录入.py。

用法（在项目根目录执行）：
    uv run --cache-dir .uv-cache python eval/judge.py               # 裁判所有已跑完的题
    uv run --cache-dir .uv-cache python eval/judge.py --tasks T01  # 只裁判 T01
    uv run --cache-dir .uv-cache python eval/judge.py --dry        # 只生成提示词不调模型（调试用）

裁判模型默认与攻略 Agent 同一个（读 .env 的 OPENAI_MODEL）。
注意：同一模型"自己裁判自己"有偏松风险，有条件建议在 .env 里加一行换不同厂商的模型：
    JUDGE_MODEL=xxx  以及对应 JUDGE_API_KEY / JUDGE_BASE_URL
输出：
    <评估工作区>/裁判输出/T01-B.txt（开头带追问轮数，直接被 自动录入.py 读取）
"""
import argparse
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.config import required_env  # noqa: E402（顺便加载 .env）
from src.context_documents import merge_contexts  # noqa: E402

# 【修改】与跑题脚本使用相同的项目相对默认目录，保留环境变量覆盖。
EVAL_HOME = Path(os.getenv("EVAL_HOME") or PROJECT_ROOT / "result")
RUNS_DIR = EVAL_HOME / "对话记录"
OUT_DIR = EVAL_HOME / "裁判输出"
EVAL_FORMAT_VERSION = "continue-history-v1"
# 评估可复现：裁判也读同一个随机种子（run_ab.py 默认写入，这里兜底）
os.environ.setdefault("EVAL_SEED", "20261117")

JUDGE_PROMPT = """你是旅游攻略的质检员。只根据【用户需求】和【知识库资料】检查【攻略】，
不要用你自己的旅游知识补充判断。

本次评测模式：{模式}（A=未追问直接生成；B=追问后直接生成；B2=沿用 B 的完整会话，移除本轮草稿、补充二次检索资料，禁止工具后生成终版）。
无论哪种模式，都只评【攻略】本身；B2 的【知识库资料】是首轮检索与二次检索的合并去重结果，以它为准核查事实。

用户需求：{需求}
知识库资料：{资料}
攻略：{攻略}
打分表：{打分表}

请严格按下面 6 块输出。表格的列不许增删、顺序不许改，我要直接复制进 Excel。

【一、得分】
需求满足：x / y
打分表命中：x / y
行程冲突：N 处
事实性陈述：N 条
白问次数：N 次

【二、需求与打分表明细】
下表已经由系统按本题【用户完整需求清单】和【打分表】预填好全部检查项（每行一条，
“来自”列标明是用户要求还是打分表）。你必须保留每一行、每个序号、每个检查项、每个“来自”，
一个字都不许改、不许删行、不许合并行、不许新增行；只把“结果”填成 满足/部分/不满足，
在“依据”写明理由。“结果”和“依据”之外若有“待填”字样，说明你没填完，必须补上。
打分表有几条就必须有几行，用户完整需求清单有几条就必须有几行——漏行会让评估作废。
{明细骨架}

【三、事实句核查】
| 序号 | 攻略原文 | 资料里有没有依据（有/无） | 依据出处或“找不到” |

【四、硬信息核查】
| 序号 | 信息项 | 攻略里写的 | 资料里写的 | 对不对（对/错/资料没写） |

【五、行程冲突】
| 序号 | 冲突类型（时间撞车/距离走不通/没开门） | 涉及安排 | 为什么冲突 |

【六、白问核查】
| 序号 | 追问的问题 | 用户的答复 | 是否白问（是/否） | 判断依据 |

注意：
1. 每张表都至少给 1 行；某张没有内容就写“无”。
2. 拿不准的按不满足算，不要因为被检查的是 AI 就放水。
3. 不要在表格外面写解释、不要加总结段落。
4. 【用户完整需求清单】是判需求满足的唯一分母：逐条判，不论方案制作者是否被告知——
   方案满足就得分，没满足就不得分；【实际被告知的信息】只说明信息差，不要重复计数。
5. 【六、白问核查】把追问记录里的每一问列一行：追问了、用户答复了、但攻略内容没有
   体现该答复的填“是”，答复已在攻略中体现的填“否”；拿不准的填“否”。
   “白问次数”必须等于表中填“是”的行数。没有追问时写“无”、白问次数为 0。
6. 【一、得分】的“需求满足”和“打分表命中”两个分子，必须照【二、明细】逐行数出来，不许凭印象估：
   - “需求满足”分子 = 明细里“来自=用户要求/用户完整需求清单”且“结果=满足”的行数；
     分母 = 用户完整需求清单的条数。
   - “打分表命中”分子 = 明细里“来自=打分表”且“结果=满足”的行数；
     分母 = 打分表的检查项条数。
   - 两类分开数，绝不混算：打分表里的“满足”只计入打分表命中，用户要求里的“满足”只计入需求满足，
     任何一方都不许挪到另一方、不许重复计数。“部分”不算“满足”。
   - 写完【二】后回头核对一次：需求满足分子是否等于明细中用户要求满足行数、
     打分表命中分子是否等于明细中打分表满足行数；对不上就改【一、得分】。"""


def _table_rows(text: str, marker: str, ncols: int) -> list[list[str]]:
    """取【marker】块内的表格数据行（跳标题/表头/分隔线，遇下一块即停）。"""
    rows: list[list[str]] = []
    idx = text.find(marker)
    if idx < 0:
        return rows
    first = True
    for line in text[idx: idx + 6000].splitlines():
        s = line.strip()
        if first:
            first = False
            continue
        if s.startswith("【"):
            break
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < ncols or cells[0] in ("序号", "编号"):
            continue
        if all(set(c) <= set("-—: ") for c in cells):
            continue
        rows.append(cells[:ncols])
    return rows


def recompute_score_block(output: str) -> tuple[str, list[str]]:
    """用六块明细重算【一、得分】，返回(修正后文本, 修改说明列表)。

    LLM 数数不可靠，落盘前用代码从明细逐行数出各分子，替换得分块对应行；
    只有能解析到明细的行才重算，解析不到就保留原值并记录提示。
    """
    notes: list[str] = []
    if "【一、得分】" not in output:
        return output, notes

    # 需求满足 / 打分表命中：来自【二】明细
    detail = _table_rows(output, "【二、需求与打分表明细】", 5)
    if detail:
        def is_user(src: str) -> bool:
            return ("用户" in src) or ("需求清单" in src)

        need_rows = [c for c in detail if is_user(c[2])]
        score_rows = [c for c in detail if "打分表" in c[2]]
        need_hit = sum(1 for c in need_rows if c[3] == "满足")
        score_hit = sum(1 for c in score_rows if c[3] == "满足")
        if need_rows:
            notes.append(f"需求满足 {need_hit}/{len(need_rows)}")
        if score_rows:
            notes.append(f"打分表命中 {score_hit}/{len(score_rows)}")

        def repl_fraction(label: str, hit: int, total: int) -> None:
            nonlocal output
            pat = re.compile(rf"({re.escape(label)}[:：]\s*)\d+\s*/\s*\d+")
            new_val = f"{hit} / {total}"
            output, n = pat.subn(lambda m: m.group(1) + new_val, output, count=1)
            if n == 0:
                notes.append(f"⚠ 未找到【{label}】行，未改")

        if need_rows:
            repl_fraction("需求满足", need_hit, len(need_rows))
        if score_rows:
            repl_fraction("打分表命中", score_hit, len(score_rows))

    # 行程冲突：来自【五】表行数（空表返回 0）
    if "【五、行程冲突】" in output:
        n_conf = len(_table_rows(output, "【五、行程冲突】", 3))
        output, n = re.subn(r"(行程冲突[:：]\s*)\d+\s*处",
                            lambda m: m.group(1) + f"{n_conf} 处", output, count=1)
        notes.append(f"行程冲突 {n_conf} 处" if n else "⚠ 未找到【行程冲突】行，未改")

    # 事实性陈述：来自【三】表行数
    if "【三、事实句核查】" in output:
        n_fact = len(_table_rows(output, "【三、事实句核查】", 4))
        output, n = re.subn(r"(事实性陈述[:：]\s*)\d+\s*条",
                            lambda m: m.group(1) + f"{n_fact} 条", output, count=1)
        notes.append(f"事实性陈述 {n_fact} 条" if n else "⚠ 未找到【事实性陈述】行，未改")

    # 白问次数：来自【六】表"是"的行数
    if "【六、白问核查】" in output:
        wq = _table_rows(output, "【六、白问核查】", 5)
        n_yes = sum(1 for c in wq if c[3] == "是")
        output, n = re.subn(r"(白问次数[:：]\s*)\d+\s*次",
                            lambda m: m.group(1) + f"{n_yes} 次", output, count=1)
        notes.append(f"白问次数 {n_yes} 次" if n else "⚠ 未找到【白问次数】行，未改")

    return output, notes


def load_checklist(csv_path: Path) -> dict[str, str]:
    """从任务集 CSV 读每题的打分表。"""
    import csv
    result = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            result[row["题号"].strip().upper()] = row[
                "打分表（好攻略必须做到的点；用分号隔开；几条就是分母）"].strip()
    return result


def load_hidden(csv_path: Path) -> dict[str, list[str]]:
    """从任务集 CSV 读每题藏起来的信息（用户心里的完整需求清单）。"""
    import csv
    result = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            raw = row["藏起来的信息（Agent追问时你照着这些回答；别给Agent看）"].strip()
            items = [s.strip() for s in raw.split(";") if s.strip() and s.strip() != "无"]
            result[row["题号"].strip().upper()] = items
    return result


def parse_run(path: Path) -> dict:
    """解析 run_ab.py 产出的 md 文件。"""
    text = path.read_text(encoding="utf-8")
    def grab(pattern):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else ""
    # 问答可能跨行（模拟用户 AI 不保证单行），用非贪婪跨行匹配，
    # 以"下一个问题/【最终攻略】/文末"为边界，避免截断多行的回答。
    qa = [
        (m.group(2).strip(), m.group(3).strip())
        for m in re.finditer(
            r"问题(\d+)：([\s\S]*?)\n回答\1：([\s\S]*?)(?=\n问题\d+：|\n【最终攻略】|\Z)",
            text,
        )
    ]
    return {
        "追问轮数": grab(r"追问轮数：(\d+)") or "0",
        "题目": grab(r"(?m)^题目：([\s\S]*?)\n【追问记录】(?:无)?(?:\n|$)"),
        "问答": qa,
        "攻略": grab(r"【最终攻略】\n?([\s\S]*)").strip(),
        # B2 要优先匹配，否则 "B2" 会被 ([AB]) 截成 "B"
        "模式": (grab(r"模式：(B2|A|B)") or ""),
        "流程版本": grab(r"(?m)^流程版本：([^\n]+)"),
        "运行标识": grab(r"(?m)^运行标识：([^\n]+)"),
    }


def load_context(stem: str, runs_dir: Path) -> str:
    """按模式加载裁判资料。

    终版攻略是用「首轮 + 二轮检索」的合并资料生成的，所以 B2 裁判也必须看
    合并后的资料，否则首轮资料支持的事实会被误判成“无依据”。
    A/B1 只看首轮 context。
    """
    tid = stem.split("-")[0]
    mode = stem.split("-")[1] if "-" in stem else ""

    def read(name: str) -> str:
        p = runs_dir / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # 新结果按模式保存首轮资料，避免其他模式后跑时覆盖共享文件；旧资料仍兼容。
    first_mode = "B" if mode.upper() == "B2" else mode.upper()
    own_context = runs_dir / f"{tid}-{first_mode}-context.txt"
    blocks = [own_context.read_text(encoding="utf-8") if own_context.exists() else read(f"{tid}-context.txt")]
    if mode.upper() == "B2":
        blocks.append(read(f"{tid}-B2-context2.txt"))

    # 【修复】与 Agent 使用同一规则，不能删除不同景点各自的重复事实行。
    return merge_contexts(*blocks) or "（无资料）"


def result_problem(path: Path, run: dict) -> str:
    """【新增】当前流程的失败结果、旧版 B2 或不配对结果不交给裁判。"""
    if path.with_suffix(".error.txt").exists():
        return "最近一次运行失败，保留的旧结果不参与本次裁判"
    if run["模式"] != "B2":
        return ""
    if run["流程版本"] != EVAL_FORMAT_VERSION or not run["运行标识"]:
        return "旧版 B2 没有完整会话续写的版本/运行标识，请先重跑 B、B2"
    b_path = path.with_name(path.stem.split("-")[0] + "-B.md")
    if not b_path.exists() or b_path.with_suffix(".error.txt").exists():
        return "配对的 B 结果缺失或最近运行失败，请先重跑 B、B2"
    b = parse_run(b_path)
    if (
        b["流程版本"] != EVAL_FORMAT_VERSION or b["运行标识"] != run["运行标识"]
        or b["题目"] != run["题目"]
    ):
        return "B 与 B2 的运行标识或题目不配对，请先重跑 B、B2"
    return ""


def archive_invalid_output(stem: str) -> Path | None:
    """保留旧评分供追溯，并移出自动录入扫描的 .txt 文件集合。"""
    output = OUT_DIR / f"{stem}.txt"
    if not output.exists():
        return None
    output_root = OUT_DIR.resolve()
    archive_dir = OUT_DIR / "历史结果"
    target = archive_dir / f"{stem}-{uuid4().hex}.txt.archived"
    # 【保护】移动前确认源和目标都在明确的评分输出目录内，不处理符号链接。
    if (
        output.is_symlink() or not output.is_file()
        or not output.resolve().is_relative_to(output_root)
        or not target.resolve().is_relative_to(output_root)
    ):
        raise ValueError("旧裁判结果的归档路径不在评分输出目录内，停止归档。")
    archive_dir.mkdir(parents=True, exist_ok=True)
    output.rename(target)
    return target


def build_user_requirement(run: dict, hidden: list[str]) -> str:
    """用户需求 = 完整需求清单（D 列，A/B 同口径）+ 标注哪些实际被告知了。"""
    parts = [f"题目原话：{run['题目']}"]
    if hidden:
        parts.append(
            "【用户完整需求清单】（用户心里的全部真实需求；判断需求满足时"
            "分母按此清单逐条判，不论方案制作者是否被告知）："
        )
        parts += [f"- {h}" for h in hidden]
    else:
        parts.append("（用户没有藏起来的额外需求，题目原话即全部需求）")
    if run["问答"]:
        qa = "；".join(f"问「{q}」→答「{a}」" for q, a in run["问答"])
        parts.append(f"【方案制作者实际被告知的信息（追问记录）】{qa}")
    else:
        parts.append("【方案制作者实际被告知的信息】无——除题目原话外用户什么都没说（追问记录为空，白问次数应为 0）")
    return "\n".join(parts)


def build_detail_skeleton(hidden: list[str], checklist: str) -> str:
    """按本题 CSV 预填【二、明细】的检查项行：需求逐条 + 打分表逐条。

    让裁判不必自己抄检查项，从源头避免漏行；裁判只需填“结果”和“依据”两列。
    """
    lines = ["| 序号 | 检查项 | 来自（用户要求/打分表） | 结果（满足/部分/不满足） | 依据 |",
             "|---|---|---|---|---|"]
    n = 0
    for h in hidden:
        if not h or h == "无":
            continue
        n += 1
        lines.append(f"| {n} | {h} | 用户完整需求清单 | 待填 | 待填 |")
    for item in [s.strip() for s in checklist.split(";") if s.strip() and s.strip() != "（未找到）"]:
        n += 1
        lines.append(f"| {n} | {item} | 打分表 | 待填 | 待填 |")
    return "\n".join(lines)


def main(tasks_arg: str, dry: bool) -> None:
    csv_path = EVAL_HOME / "评估任务集模板.csv"
    checklists = load_checklist(csv_path)
    hidden_map = load_hidden(csv_path)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # T??-?.md 只匹配单字符模式；改成同时匹配 T01-A/B/B2，但不吃 draft
    files = sorted(
        f for f in RUNS_DIR.glob("T??-*.md") if "-draft" not in f.stem
    )
    if not files:
        print(f"{RUNS_DIR} 里没有跑题结果，请先运行 eval/run_ab.py")
        sys.exit(1)

    want = [t.strip().upper() for t in tasks_arg.split(",") if t.strip()]
    targets = [f for f in files if not want or f.stem.upper().startswith(tuple(want))]
    if not targets:
        print(f"没有匹配的题：{want}")
        sys.exit(1)

    valid_targets = []
    for path in targets:
        problem = result_problem(path, parse_run(path))
        if problem:
            print(f"跳过 {path.name}：{problem}")
            if not dry:
                archived = archive_invalid_output(path.stem)
                if archived is not None:
                    print(f"  已归档旧裁判结果：{archived}")
        else:
            valid_targets.append(path)
    targets = valid_targets
    if not targets:
        print("没有可裁判的成功配对结果，请先重新运行评测。")
        return

    if dry:
        for f in targets:
            run = parse_run(f)
            _tid = f.stem.split("-")[0]
            _hidden = hidden_map.get(_tid, [])
            _checklist = checklists.get(_tid, "（未找到）")
            prompt = JUDGE_PROMPT.format(
                模式=run["模式"] or "B",
                需求=build_user_requirement(run, _hidden),
                资料=load_context(f.stem, RUNS_DIR),
                攻略=run["攻略"], 打分表=_checklist.replace(";", "\n"),
                明细骨架=build_detail_skeleton(_hidden, _checklist))
            (RUNS_DIR / f"{f.stem}-裁判提示词.txt").write_text(prompt, encoding="utf-8")
        print(f"已生成 {len(targets)} 份裁判提示词到 {RUNS_DIR}（--dry 模式未调用模型）")
        return

    from openai import OpenAI
    client = OpenAI(
        api_key=os.getenv("JUDGE_API_KEY") or required_env("OPENAI_API_KEY"),
        base_url=os.getenv("JUDGE_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None,
    )
    model = os.getenv("JUDGE_MODEL") or os.getenv("OPENAI_MODEL") or "deepseek-v4-pro"

    for f in targets:
        run = parse_run(f)
        if not run["攻略"]:
            print(f"跳过 {f.name}：没有攻略内容")
            continue
        context = load_context(f.stem, RUNS_DIR)
        _tid = f.stem.split("-")[0]
        _hidden = hidden_map.get(_tid, [])
        _checklist = checklists.get(_tid, "（未找到）")
        prompt = JUDGE_PROMPT.format(
            模式=run["模式"] or "B",
            需求=build_user_requirement(run, _hidden), 资料=context,
            攻略=run["攻略"],
            打分表=_checklist.replace(";", "\n"),
            明细骨架=build_detail_skeleton(_hidden, _checklist),
        )
        print(f"裁判 {f.stem}（模型 {model}）……", flush=True)
        try:
            resp = client.chat.completions.create(
                model=model, temperature=0, seed=int(os.getenv("EVAL_SEED", "20261117")),
                messages=[{"role": "user", "content": prompt}],
            )
            output = resp.choices[0].message.content or ""
        except Exception as exc:
            print(f"  失败：{exc}")
            continue
        # 落盘前用明细重算【一、得分】，覆盖 LLM 数错的数字（LLM 只负责语义判断）
        output, notes = recompute_score_block(output)
        if notes:
            print(f"  得分块已按明细重算：{'、'.join(notes)}")
        out_file = OUT_DIR / f"{f.stem}.txt"
        out_file.write_text(f"追问轮数：{run['追问轮数']}\n\n{output}", encoding="utf-8")
        print(f"  已保存 {out_file}")

    print(f"\n裁判完成。下一步：到评估工作区运行 python 自动录入.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="攻略 Agent 自动裁判")
    parser.add_argument("--tasks", default="", help="逗号分隔的题号；默认全部已跑完的")
    parser.add_argument("--dry", action="store_true", help="只生成提示词不调模型")
    args = parser.parse_args()
    main(args.tasks, args.dry)
