# -*- coding: utf-8 -*-
"""自动录入脚本：把裁判输出的 txt 自动填进《攻略Agent评估成绩单.xlsx》

用法：
1. 每份攻略的裁判输出存成一个 txt，放进同目录的「裁判输出」文件夹
   文件名：题号-模式.txt，例如 T01-A.txt、T01-B.txt、T01-B2.txt
   模式支持 A、B、B2 三种：
   - A：只填成绩单数字列
   - B：填数字列 + 瞎编核查 + 硬信息核查 + 白问核查
   - B2：填数字列 + 瞎编核查B2 + 硬信息核查B2；白问不重复登记（与 B 相同）
2. 裁判输出共 6 块：白问次数在【一、得分】里给总数、在【六、白问核查】里给明细，
   都不用手写。
3. 双击运行或在命令行执行：python 自动录入.py
4. 每次运行会先清掉该题在核查表里的旧记录再写入，所以重复跑同一题不会重复登记。

只自动填：成绩单的数字列（白问次数由白问核查表自动数，不直接填）
          + 三张核查表的句子与 AI 初判。
你仍需人工做：改核查表里的终判列（有/无/对/错/是否白问）、抽查裁判打分。
"""
import re
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "openpyxl>=3.1.0"])
    import openpyxl

BASE = Path(__file__).parent
XLSX = BASE / "攻略Agent评估成绩单.xlsx"
SRC_DIR = BASE / "裁判输出"
TASKS_CSV = BASE / "评估任务集模板.csv"
CHECK_FIRST, CHECK_LAST = 2, 601  # 核查表数据区预留 600 行（30 题 × 每题最多约 15~20 条，留足余量）


def parse_file(path: Path):
    text = path.read_text(encoding="utf-8")
    meta = {"追问轮数": None, "白问次数": None}
    m = re.search(r"追问轮数[:：]\s*(\d+)", text)
    if m:
        meta["追问轮数"] = int(m.group(1))
    m = re.search(r"白问次数[:：]\s*(\d+)", text)
    if m:
        meta["白问次数"] = int(m.group(1))

    score = {}
    m = re.search(r"需求满足[:：]\s*(\d+)\s*/\s*(\d+)", text)
    if m:
        score["满足条数"], score["要求总数"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"打分表命中[:：]\s*(\d+)\s*/\s*(\d+)", text)
    if m:
        score["命中条数"], score["要点总数"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"行程冲突[:：]\s*(\d+)\s*处", text)
    if m:
        score["冲突数"] = int(m.group(1))
    m = re.search(r"事实性陈述[:：]\s*(\d+)\s*条", text)
    if m:
        score["事实总数"] = int(m.group(1))
    return meta, score, text


def parse_table(text: str, marker: str, ncols: int):
    """提取【marker】块后面的 markdown 表格，返回行列表（去掉表头和分隔线）。

    只取 marker 标题到下一个【X、】标题之间的内容：
    - 跳过 marker 标题行本身；
    - 一旦遇到下一个块标题立即停，哪怕本块一个表格行都没抓到
      （否则【五】写"无"时会把后面【六】的表串进来）。
    """
    rows = []
    idx = text.find(marker)
    if idx < 0:
        return rows
    chunk = text[idx: idx + 6000]
    first = True
    for line in chunk.splitlines():
        s = line.strip()
        if first:  # 跳过 marker 标题行本身
            first = False
            continue
        if s.startswith("【"):  # 进入下一块，立即停（本块无表格也停）
            break
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < ncols:
                continue
            if all(set(c) <= set("-—: ") for c in cells):  # 分隔线
                continue
            if cells[0] in ("序号", "编号"):  # 表头
                continue
            rows.append(cells[:ncols])
        elif s and rows:  # 表格已开始，遇到非表格文字则停
            break
    return rows


def reconcile_scores(text: str, score: dict, label: str):
    """核对【一、得分】的两个分子/分母与【二、明细】是否一致。

    明细表列：序号 | 检查项 | 来自（用户要求/打分表） | 结果（满足/部分/不满足） | 依据
    - 需求满足分子 = 来自“用户要求/用户完整需求清单”且结果“满足”的行数
    - 打分表命中分子 = 来自“打分表”且结果“满足”的行数
    若与得分块不一致：打印警告，并改用明细重算值（明细是逐条判定，更可信）。
    """
    detail = parse_table(text, "【二、需求与打分表明细】", 5)
    if not detail:
        return score  # 明细解析不到就保留得分块原值，不做改动

    def is_user(src: str) -> bool:
        return ("用户" in src) or ("需求清单" in src)

    def is_score(src: str) -> bool:
        return "打分表" in src

    # 只统计结果精确等于“满足”的行；“部分”不算满足
    need_hit = sum(1 for c in detail if is_user(c[2]) and c[3] == "满足")
    score_hit = sum(1 for c in detail if is_score(c[2]) and c[3] == "满足")
    need_total = sum(1 for c in detail if is_user(c[2]))
    score_total = sum(1 for c in detail if is_score(c[2]))

    warnings = []
    # 需求满足：分子照明细重算，分母用明细里"用户来源"的行数（与清单条数应一致）
    if "满足条数" in score:
        if need_total and score["满足条数"] != need_hit:
            warnings.append(
                f"需求满足分子：得分块={score['满足条数']}，明细数出={need_hit}，已改用明细值")
        if need_total and score["要求总数"] != need_total:
            warnings.append(
                f"需求满足分母：得分块={score['要求总数']}，明细用户项={need_total}，已改用明细值")
        if need_total:
            score["满足条数"], score["要求总数"] = need_hit, need_total
    # 打分表命中：分子照明细重算
    if "命中条数" in score:
        if score_total and score["命中条数"] != score_hit:
            warnings.append(
                f"打分表命中分子：得分块={score['命中条数']}，明细数出={score_hit}，已改用明细值")
        if score_total and score["要点总数"] != score_total:
            warnings.append(
                f"打分表命中分母：得分块={score['要点总数']}，明细打分表项={score_total}，已改用明细值")
        if score_total:
            score["命中条数"], score["要点总数"] = score_hit, score_total

    # 行程冲突：改由【五】表明细行数重算（唯一还直接采信 LLM 得分块的数字）
    conflicts = parse_table(text, "【五、行程冲突】", 3)
    if "冲突数" in score and "【五、行程冲突】" in text:
        n_conf = len(conflicts)
        if score["冲突数"] != n_conf:
            warnings.append(
                f"行程冲突数：得分块={score['冲突数']}，明细数出={n_conf}，已改用明细值")
        score["冲突数"] = n_conf

    for w in warnings:
        print(f"  ⚠ 核对[{label}]：{w}")
    return score


def clear_task_rows(ws, task_id: str):
    """清掉该题号已有的登记行（保留行位置，只清内容）。返回清掉的行数。"""
    n = 0
    for r in range(CHECK_FIRST, CHECK_LAST + 1):
        if ws.cell(row=r, column=1).value == task_id:
            for c in range(1, ws.max_column + 1):
                ws.cell(row=r, column=c).value = None
            n += 1
    return n


def next_free_row(ws) -> int:
    last = CHECK_FIRST
    for r in range(CHECK_FIRST, CHECK_LAST + 1):
        if any(ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)):
            last = r + 1
    return last


def compact_sheet(ws) -> int:
    """把核查表数据从第2行起压实连续排列，消除中间的空洞行（只挪不删）。

    - 扫描数据区 CHECK_FIRST..CHECK_LAST，凡 A 列有题号的行整行读进内存；
    - 清空数据区内容；
    - 从第 CHECK_FIRST 行起按原顺序写回；
    - 前后数一遍行数，不一致则抛异常阻止保存（防止误删）。

    不动表头（第1行）、底部说明行（CHECK_LAST+2）、列宽/冻结/下拉/公式。
    返回压实后的数据行数。
    """
    rows = []
    for r in range(CHECK_FIRST, CHECK_LAST + 1):
        if ws.cell(row=r, column=1).value not in (None, ""):
            rows.append([ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)])
    before = len(rows)

    for r in range(CHECK_FIRST, CHECK_LAST + 1):
        for c in range(1, ws.max_column + 1):
            ws.cell(row=r, column=c).value = None

    for i, vals in enumerate(rows):
        r = CHECK_FIRST + i
        for c, v in enumerate(vals, start=1):
            ws.cell(row=r, column=c).value = v

    after = sum(1 for r in range(CHECK_FIRST, CHECK_LAST + 1)
                if ws.cell(row=r, column=1).value not in (None, ""))
    if before != after:
        raise RuntimeError(f"压实校验失败：{ws.title} 压实前 {before} 行、压实后 {after} 行，已中止保存")
    return after


def main():
    if not XLSX.exists():
        print("找不到 攻略Agent评估成绩单.xlsx，请把它和本脚本放在同一文件夹")
        sys.exit(1)
    if not SRC_DIR.exists():
        SRC_DIR.mkdir()
        print("已创建文件夹「裁判输出」，请把裁判输出的 txt 放进去再运行")
        sys.exit(0)

    files = sorted(SRC_DIR.glob("*.txt"))
    if not files:
        print(f"「{SRC_DIR.name}」文件夹里没有 txt，文件名格式：T01-B.txt")
        sys.exit(0)

    wb = openpyxl.load_workbook(XLSX)
    ws = wb["成绩单"]
    ws3 = wb["瞎编核查"]
    ws4 = wb["硬信息核查"]
    ws5 = wb["白问核查"]
    # 【B2】B2 的瞎编/硬信息核查写进专用新表，避免和 B 混在一起
    ws3b = wb["瞎编核查B2"] if "瞎编核查B2" in wb.sheetnames else None
    ws4b = wb["硬信息核查B2"] if "硬信息核查B2" in wb.sheetnames else None

    # 成绩单行索引：题号+模式 -> 行号
    row_map = {}
    for r in range(2, ws.max_row + 1):
        t, m = ws.cell(row=r, column=1).value, ws.cell(row=r, column=2).value
        if t and m:
            row_map[f"{t}-{m}"] = r

    done, skipped = [], []
    for f in files:
        # B2 要放在 B 前面，否则 T01-B2.txt 会被匹配成模式 B
        m = re.match(r"(T\d+)\-(B2|A|B)\.txt", f.name, re.IGNORECASE)
        if not m:
            skipped.append(f"{f.name}（文件名不符合 题号-模式.txt）")
            continue
        task_id, mode = m.group(1).upper(), m.group(2).upper()
        key = f"{task_id}-{mode}"
        if key not in row_map:
            skipped.append(f"{f.name}（成绩单里没有 {key} 这一行）")
            continue

        meta, score, text = parse_file(f)
        r = row_map[key]

        # 0) 核对得分块 vs 明细：不一致时报警并改用明细重算值
        score = reconcile_scores(text, score, f.name)

        # 1) 成绩单数字列：C追问轮数 E满足 F要求 H命中 I要点 K冲突
        #    （白问次数 D 列不填——由「白问核查」表自动数"是"的行数）
        if meta["追问轮数"] is not None:
            ws.cell(row=r, column=3).value = meta["追问轮数"]
        if "满足条数" in score:
            ws.cell(row=r, column=5).value = score["满足条数"]
            ws.cell(row=r, column=6).value = score["要求总数"]
        if "命中条数" in score:
            ws.cell(row=r, column=8).value = score["命中条数"]
            ws.cell(row=r, column=9).value = score["要点总数"]
        if "冲突数" in score:
            ws.cell(row=r, column=11).value = score["冲突数"]

        # 2) 瞎编核查：先清旧再追加（模式 A 不登记核查表）
        #    B 写「瞎编核查」，B2 写「瞎编核查B2」，两者分开避免混淆
        fab_rows = 0
        if mode in ("B", "B2"):
            sheet_fab = ws3b if mode == "B2" and ws3b is not None else ws3
            clear_task_rows(sheet_fab, task_id)
            start = next_free_row(sheet_fab)
            table = parse_table(text, "【三、事实句核查】", 4)
            for i, cells in enumerate(table):
                r3 = start + i
                if r3 > CHECK_LAST:
                    break
                # 列：序号 | 攻略原文 | 有没有依据 | 出处
                sheet_fab.cell(row=r3, column=1).value = task_id
                sheet_fab.cell(row=r3, column=2).value = cells[1]
                sheet_fab.cell(row=r3, column=3).value = cells[2]
                sheet_fab.cell(row=r3, column=4).value = f"AI初判，出处：{cells[3]}"
                fab_rows += 1

        # 3) 硬信息核查
        hard_rows = 0
        if mode in ("B", "B2"):
            sheet_hard = ws4b if mode == "B2" and ws4b is not None else ws4
            clear_task_rows(sheet_hard, task_id)
            start = next_free_row(sheet_hard)
            table = parse_table(text, "【四、硬信息核查】", 5)
            for i, cells in enumerate(table):
                r4 = start + i
                if r4 > CHECK_LAST:
                    break
                # 列：序号 | 信息项 | 攻略里写的 | 资料里写的 | 对不对
                sheet_hard.cell(row=r4, column=1).value = task_id
                sheet_hard.cell(row=r4, column=2).value = cells[1]
                sheet_hard.cell(row=r4, column=3).value = cells[2]
                sheet_hard.cell(row=r4, column=4).value = cells[3]
                sheet_hard.cell(row=r4, column=5).value = cells[4]
                hard_rows += 1

        # 4) 白问核查（每轮追问一行，AI 初判预填"是/否"）
        wq_rows = 0
        if mode == "B":
            clear_task_rows(ws5, task_id)
            start = next_free_row(ws5)
            table = parse_table(text, "【六、白问核查】", 5)
            for i, cells in enumerate(table):
                r5 = start + i
                if r5 > CHECK_LAST:
                    break
                # 列：序号 | 追问的问题 | 用户的答复 | 是否白问（是/否） | 判断依据
                ws5.cell(row=r5, column=1).value = task_id
                ws5.cell(row=r5, column=2).value = "B"
                ws5.cell(row=r5, column=3).value = cells[1]
                ws5.cell(row=r5, column=4).value = cells[2]
                ws5.cell(row=r5, column=5).value = cells[3]
                ws5.cell(row=r5, column=6).value = f"AI初判：{cells[4]}"
                wq_rows += 1
            # 一致性检查：块一的白问次数 vs 表六"是"的行数
            n_yes = sum(1 for row_cells in parse_table(text, "【六、白问核查】", 5) if row_cells[3] == "是")
            if meta["白问次数"] is not None and n_yes != meta["白问次数"]:
                done.append(f"  注意 {f.name}：块一白问次数={meta['白问次数']}，但表六'是'共 {n_yes} 行，请人工核对")
            if not table and meta["白问次数"]:
                done.append(f"  注意 {f.name}：表六没有明细行，但块一白问次数={meta['白问次数']}，请人工补录白问核查表")

        need_keys = ("满足条数", "命中条数", "冲突数")
        miss = [k for k in need_keys if k not in score]
        done.append(f"{f.name}：成绩单数字列已填"
                    + (f"；瞎编核查 +{fab_rows} 行" if mode in ("B", "B2") else "")
                    + (f"；硬信息核查 +{hard_rows} 行" if mode in ("B", "B2") else "")
                    + (f"；白问核查 +{wq_rows} 行" if mode == "B" else "")
                    + (f"（白问与B相同，不重复登记）" if mode == "B2" else "")
                    + (f"（注意：{ '、'.join(miss) } 没解析到，需手动补）" if miss else ""))

    # 5) 压实：把五张核查表的数据从第2行起连续排，消除历史遗留空洞行（只挪不删）
    compact_targets = [ws3, ws4, ws5] + [s for s in (ws3b, ws4b) if s is not None]
    compact_results = []
    for sheet in compact_targets:
        try:
            n = compact_sheet(sheet)
            compact_results.append(f"{sheet.title} {n}行")
        except RuntimeError as exc:
            print(f"  ⚠ {exc}")
            sys.exit(1)
    print("  已压实核查表：" + "、".join(compact_results))

    wb.save(XLSX)
    print("=== 录入完成 ===")
    for d in done:
        print(" ", d)
    for s in skipped:
        print("  跳过：", s)
    print("提醒：核查表里“AI初判”的结论只是裁判的意见，"
          "记得逐条核成你自己的最终判定（瞎编列重点核“无”，硬信息重点核“错”）。")


if __name__ == "__main__":
    main()
