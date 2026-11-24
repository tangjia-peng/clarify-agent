"""在项目根目录执行 uv run main.py，也可以 uv run python -m src.index。"""

from src.index import run_cli


if __name__ == "__main__":
    # 【修改】【删除说明】移除重复的单次任务参数解析，复用多轮对话入口。
    run_cli()
