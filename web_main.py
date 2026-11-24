"""【新增】运行 uv run web_main.py，在浏览器访问 http://127.0.0.1:8765。"""

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 Agent 本地聊天界面")
    from src.web_security import WebSettings

    parser.add_argument("--host", choices=["127.0.0.1", "localhost", "0.0.0.0"], default=os.getenv("WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")), help="服务端口，默认读取 PORT 或使用 8765")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    try:
        settings = WebSettings.from_env()
    except ValueError as exc:
        parser.error(str(exc))
    if args.host == "0.0.0.0" and not settings.public:
        parser.error("对外监听需要设置 WEB_PUBLIC_URL 和 WEB_ACCESS_PASSWORD")
    address = settings.public_url or f"http://{args.host}:{args.port}"
    print(f"\nMellow 工作空间：{address}\n")
    uvicorn.run("src.web_app:app", host=args.host, port=args.port, ws_max_size=256_000, timeout_graceful_shutdown=15)


if __name__ == "__main__":
    main()
