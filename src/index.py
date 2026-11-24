"""【修改】读取知识库，逐轮检索上下文，运行带会话记忆的终端对话。"""

import argparse
import asyncio
import os
from pathlib import Path

from .agent import Agent, SessionExit, TaskCancelled
from .config import PROJECT_ROOT
from .context_documents import join_context_documents
from .embedding_retriever import EmbeddingRetriever
from .mcp_client import MCPClient
from .utils import log_title


def create_mcp_clients(*, file_root: Path | None = None, allow_private_ips: bool | None = None) -> list[MCPClient]:
    # 【修改】MCP 子进程也使用可写缓存，保留用户显式设置的缓存路径。
    fetch_args = [
        "--cache-dir", os.getenv("UV_CACHE_DIR") or str(PROJECT_ROOT / ".uv-cache"),
        "mcp-server-fetch",
    ]
    fetch_command = os.getenv("MCP_FETCH_COMMAND", "uvx")
    if fetch_command != "uvx":
        fetch_args = []
    if os.getenv("MCP_FETCH_NO_JS", "false").lower() in {"1", "true", "yes"}:
        fetch_args.append("--no-readability-js")
    if allow_private_ips is None:
        allow_private_ips = os.getenv("FETCH_ALLOW_PRIVATE_IPS", "false").lower() in {"1", "true", "yes"}
    if allow_private_ips:
        fetch_args.append("--allow-private-ips")
    directory = (file_root or PROJECT_ROOT).resolve()
    file_command = os.getenv("MCP_FILESYSTEM_COMMAND", "npx")
    file_args = [str(directory)] if file_command != "npx" else [
        "--cache", os.getenv("npm_config_cache") or str(PROJECT_ROOT / ".npm-cache"),
        "-y", "@modelcontextprotocol/server-filesystem", str(directory),
    ]
    return [
        MCPClient("fetch", fetch_command, fetch_args, cwd=directory),
        MCPClient("file", file_command, file_args, cwd=directory),
    ]


# 【新增】每场对话只构建一次文档向量库，后续提问复用。
async def create_retriever(knowledge_dir: Path | None = None) -> EmbeddingRetriever:
    retriever = EmbeddingRetriever(os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"))
    directory = knowledge_dir or PROJECT_ROOT / "knowledge"
    if not directory.is_dir():
        raise FileNotFoundError(f"知识库目录不存在：{directory}")
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in {".md", ".txt"})
    if not files:
        raise ValueError(f"知识库没有 .md 或 .txt 文档：{directory}")
    for file in files:
        content = file.read_text(encoding="utf-8-sig")
        if content.strip():
            await retriever.embed_document(content)
    return retriever


# 【修改】保留原来的独立检索入口，也支持复用会话已有的检索器。
async def retrieve_context(
    prompt: str, knowledge_dir: Path | None = None,
    *, retriever: EmbeddingRetriever | None = None,
) -> str:
    if retriever is None:
        retriever = await create_retriever(knowledge_dir)
    documents = await retriever.retrieve(prompt)
    log_title("CONTEXT")
    # 【修复】保留每篇文档的边界，供 B2 与裁判按整篇去重。
    context = join_context_documents(documents)
    print(context)
    return context


# 【新增】在线程中等待终端输入，避免阻塞 MCP 的异步通信。
async def read_prompt() -> str | None:
    while True:
        try:
            prompt = (await asyncio.to_thread(input, "\n你：")).strip()
        except EOFError:
            return None
        if prompt.lower() in {"exit", "quit", "/exit", "/quit"}:
            return None
        if prompt.lower() == "/cancel":
            print("当前没有正在等待确认的任务，请输入新问题。")
            continue
        if prompt:
            return prompt
        print("请输入问题，或输入 /exit 退出。")


# 【新增】执行中途的终端交互；通过回调传入 Agent，用户回复作为工具结果返回。
async def ask_in_terminal(question: str) -> str:
    print(f"\nAgent 询问：{question}")
    while True:
        try:
            answer = (await asyncio.to_thread(
                input, "你的回复（/cancel 取消当前任务，/exit 退出）：",
            )).strip()
        except EOFError:
            raise SessionExit from None
        if answer.lower() in {"/exit", "/quit", "exit", "quit"}:
            raise SessionExit
        if answer.lower() == "/cancel":
            raise TaskCancelled
        if answer:
            return answer
        print("请输入答复，或输入 /cancel 取消当前任务。")


# 【修改】从一次任务改为多轮会话；--prompt 只提供首轮问题。
async def main(prompt: str | None = None) -> str:
    print("多轮对话已启动；Agent 可中途询问。询问时 /cancel 取消当前任务，/exit 退出。记忆仅保留在本次运行中。")
    # 【删除说明】取消原来的默认 Bret 故事任务；未传 --prompt 时等待用户输入。
    prompt = prompt.strip() if prompt else ""
    if not prompt:
        prompt = await read_prompt()
    if prompt is None:
        return ""
    retriever = await create_retriever()
    # 【B2】本轮草稿生成后，用「首问 + 本轮追问问答」二次检索知识库，供终版阶段合并使用。
    # 复用本场会话的 retriever 与检索函数，与首轮检索同源，口径与 eval 的 finalize_context 一致。
    async def finalize_context(query: str) -> str:
        return await retrieve_context(query, retriever=retriever)

    # 【修改】把终端函数作为回调传入；Agent 自身不直接读取键盘输入。
    # 【B2】传入 finalize_context 后，Agent 每轮会先出草稿、再二次检索生成终版。
    agent = Agent(
        os.getenv("OPENAI_MODEL", "deepseek-v4-pro"), create_mcp_clients(),
        ask_user_callback=ask_in_terminal,
        finalize_context=finalize_context,
    )
    last_response = ""
    try:
        await agent.init()
        while prompt is not None:
            try:
                context = await retrieve_context(prompt, retriever=retriever)
                # ChatOpenAI 已流式打印回答，此处不再重复打印全文。
                last_response = await agent.invoke(prompt, context=context)
            except SessionExit:
                break
            except TaskCancelled:
                print("当前任务已取消，可以输入新任务。已执行的工具操作不会被撤销。")
            except Exception as exc:
                print(f"本轮失败：{exc}\n可以继续提问。已执行的工具操作不会被撤销。")
            prompt = await read_prompt()
    finally:
        # 【新增】正常退出、输入结束和异常退出均在此关闭整场会话的资源。
        await agent.close()
    return last_response


# 【新增】两个启动入口共用参数解析，保证行为一致。
def run_cli() -> None:
    parser = argparse.ArgumentParser(description="运行支持会话记忆的 LLM + MCP + RAG 多轮对话")
    parser.add_argument("--prompt", help="可选：首轮问题；回答后继续等待输入")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.prompt))
    except KeyboardInterrupt:
        print("\n对话已结束。")


if __name__ == "__main__":
    run_cli()
