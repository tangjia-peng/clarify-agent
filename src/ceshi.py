"""对应 ceshi.ts，另提供单独验证 LLM 和 RAG 的模式。"""

import argparse
import asyncio
import json
import os

from .chat_openai import ChatOpenAI
from .index import create_mcp_clients, retrieve_context


async def main(mode: str, prompt: str) -> None:
    if mode == "llm":
        llm = ChatOpenAI(os.getenv("OPENAI_MODEL", "deepseek-v4-pro"))
        try:
            response = await llm.chat(prompt)
            print(response)
        finally:
            await llm.close()
    elif mode == "rag":
        print(await retrieve_context(prompt))
    else:
        clients = create_mcp_clients()
        selected = clients if mode == "all-tools" else clients[:1]
        opened = []
        try:
            for client in selected:
                await client.init()
                opened.append(client)
                print(json.dumps(client.get_tools(), ensure_ascii=False, indent=2))
        finally:
            for client in reversed(opened):
                await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="单独验证项目模块")
    parser.add_argument("--mode", choices=["tools", "all-tools", "llm", "rag"], default="tools")
    parser.add_argument("--prompt", default="你好")
    args = parser.parse_args()
    asyncio.run(main(args.mode, args.prompt))
