#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
command.py — Clarify Agent 项目命令速查 + 一键执行脚本

两种用法：
  1) 直接看下面「命令速查表」的注释，复制粘贴到终端；
  2) 用子命令一键执行：python command.py <子命令>

子命令：
  show      打印全部命令（只显示不执行）
  cli       启动 CLI 终端对话（B2 模式）
  web       启动 Web 网页端（B2 模式，可用 --port 改端口）
  run       评测①跑题：生成 A/B/B2 攻略到 result/对话记录/
  judge     评测②裁判：LLM 打分，输出到 result/裁判输出/
  record    评测③录入：填 result/攻略Agent评估成绩单.xlsx
  eval      评测全流程：run → judge → record（顺序执行）
  runx      仅跑题并指定题目/模式（配合 --tasks / --modes）

=====================================================================
【命令速查表】—— 以下为原始命令，可直接复制
=====================================================================

--- 一、启动产品 ---------------------------------------------------
进入项目根目录（所有产品命令都以此为基准）：
    cd "<项目目录>/clarify-agent"

CLI（终端对话）：
    uv run main.py

Web（浏览器访问 http://127.0.0.1:8765）：
    uv run web_main.py
    # 换端口：uv run web_main.py --port 8888
    # 两个入口默认都是 B2 模式（追问 + 二次检索 + 终版重生成）

--- 二、评测全流程（工作区已切到 result，无需再指定路径）---------

① 跑题：生成 A/B/B2 三模式攻略到 result/对话记录/
    cd "<项目目录>/clarify-agent"
    uv run --cache-dir .uv-cache python eval/run_ab.py --modes A,B,B2
    # 默认是 A,B；要三模式对照必须显式加 --modes A,B,B2
    # B2 会自动补跑它依赖的 B
    # 只跑某几题：追加 --tasks T01,T02

② 裁判：LLM 打分，输出到 result/裁判输出/
    uv run --cache-dir .uv-cache python eval/judge.py
    # 想先免费看提示词：追加 --dry
    # 建议 .env 里设 JUDGE_MODEL= 换裁判模型

③ 录入：填 result/攻略Agent评估成绩单.xlsx
    cd "<项目目录>/clarify-agent/result"
    python 自动录入.py
    # ⚠️ 这步必须用系统 python（它装了 openpyxl），
    #    不要用 uv run，否则报 No module named 'openpyxl'
    # 成绩单被 WPS/Excel 打开时先关掉，否则写不进去

--- 三、关键提醒 ---------------------------------------------------
* 评测默认已指向当前项目的 result：EVAL_HOME 默认根据脚本位置定位，
  即 Clarify Agent/result，项目移动或改名后仍跟随项目，不用手动设路径；
  旧的 D 盘工作区那份成绩单已冻结。
* 录入脚本按自身所在目录（__file__.parent）找成绩单，
  所以第③步要 cd 进 result 再跑，跑的是 result 里的那份。
* 人工只剩两件事：核查表的终判列、白问次数抽查——
  看成绩单的「A-B对比」表。
=====================================================================
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# 脚本自身所在目录即项目根目录，换机器也不用改路径
PROJECT_ROOT = Path(__file__).resolve().parent
RESULT_DIR = PROJECT_ROOT / "result"

# 评测脚本在 uv 环境跑；录入脚本必须用系统 python（uv 环境无 openpyxl）
UV_CACHE = ".uv-cache"


def _print_cmd(cmd: list[str], cwd: Path) -> None:
    # 【修改】目录名含空格时，打印出来的命令也能直接复制执行。
    print(f'\n$ cd "{cwd}"')
    print("$ " + subprocess.list2cmdline(cmd), flush=True)


def _run(cmd: list[str], cwd: Path) -> int:
    """打印并执行命令，返回退出码。"""
    _print_cmd(cmd, cwd)
    return subprocess.run(cmd, cwd=str(cwd), check=False).returncode


def cmd_cli() -> int:
    """CLI 终端对话（B2 模式）。"""
    return _run(["uv", "run", "main.py"], PROJECT_ROOT)


def cmd_web(port: int | None = None) -> int:
    """Web 网页端（B2 模式）。"""
    cmd = ["uv", "run", "web_main.py"]
    if port is not None:
        cmd += ["--port", str(port)]
    return _run(cmd, PROJECT_ROOT)


def cmd_run(tasks: str | None = None, modes: str = "A,B,B2") -> int:
    """评测①：跑题。"""
    cmd = ["uv", "run", "--cache-dir", UV_CACHE, "python",
           "eval/run_ab.py", "--modes", modes]
    if tasks:
        cmd += ["--tasks", tasks]
    return _run(cmd, PROJECT_ROOT)


def cmd_judge(dry: bool = False) -> int:
    """评测②：裁判。"""
    cmd = ["uv", "run", "--cache-dir", UV_CACHE, "python", "eval/judge.py"]
    if dry:
        cmd.append("--dry")
    return _run(cmd, PROJECT_ROOT)


def cmd_record() -> int:
    """评测③：录入。必须用系统 python，且 cwd 为 result。"""
    if not (RESULT_DIR / "自动录入.py").exists():
        print(f"[错误] 找不到录入脚本：{RESULT_DIR / '自动录入.py'}", file=sys.stderr)
        return 1
    return _run(["python", "自动录入.py"], RESULT_DIR)


def cmd_eval(tasks: str | None = None, skip_record: bool = False) -> int:
    """评测全流程：跑题 → 裁判 → 录入；任一步失败即中断。"""
    print("=" * 60)
    print("评测全流程开始（工作区：%s）" % RESULT_DIR)
    print("=" * 60)
    steps = [
        ("① 跑题", lambda: cmd_run(tasks=tasks)),
        ("② 裁判", cmd_judge),
    ]
    if not skip_record:
        steps.append(("③ 录入", cmd_record))
    for name, fn in steps:
        print(f"\n>>> {name}")
        rc = fn()
        if rc != 0:
            print(f"[中断] {name} 失败，退出码 {rc}", file=sys.stderr)
            return rc
    print("\n评测全流程完成。请人工核查成绩单的「A-B对比」表。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clarify Agent 项目命令速查 + 一键执行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不带参数时打印命令速查表。",
    )
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("show", help="打印全部命令（只显示不执行）")
    sub.add_parser("cli", help="启动 CLI 终端对话")

    p_web = sub.add_parser("web", help="启动 Web 网页端")
    p_web.add_argument("--port", type=int, default=None, help="端口，默认 8765")

    p_run = sub.add_parser("run", help="评测①跑题")
    p_run.add_argument("--tasks", default=None, help="只跑指定题，如 T01,T02")
    p_run.add_argument("--modes", default="A,B,B2", help="模式，默认 A,B,B2")

    p_judge = sub.add_parser("judge", help="评测②裁判")
    p_judge.add_argument("--dry", action="store_true", help="只打印提示词，不调用模型")

    sub.add_parser("record", help="评测③录入（系统 python）")

    p_eval = sub.add_parser("eval", help="评测全流程：run → judge → record")
    p_eval.add_argument("--tasks", default=None, help="只跑指定题，如 T01,T02")
    p_eval.add_argument("--skip-record", action="store_true", help="跳过录入步骤")

    p_runx = sub.add_parser("runx", help="仅跑题并指定题目/模式（同 run）")
    p_runx.add_argument("--tasks", default=None)
    p_runx.add_argument("--modes", default="A,B,B2")

    args = parser.parse_args()

    if args.action in (None, "show"):
        print(__doc__)
        return 0
    if args.action == "cli":
        return cmd_cli()
    if args.action == "web":
        return cmd_web(args.port)
    if args.action in ("run", "runx"):
        return cmd_run(tasks=args.tasks, modes=args.modes)
    if args.action == "judge":
        return cmd_judge(dry=args.dry)
    if args.action == "record":
        return cmd_record()
    if args.action == "eval":
        return cmd_eval(tasks=args.tasks, skip_record=args.skip_record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
