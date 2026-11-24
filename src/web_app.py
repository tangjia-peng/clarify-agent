"""【新增】本地聊天网页后端：每个 WebSocket 拥有独立的 Agent 会话。"""

import asyncio
from contextlib import suppress
from html import escape
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

import anyio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .agent import Agent, SessionExit, TaskCancelled
from .config import PROJECT_ROOT
from .index import create_mcp_clients, create_retriever, retrieve_context
from .web_security import COOKIE_NAME, LoginStore, WebSettings, canonical_origin


MAX_MESSAGE_CHARS = 50_000


def same_origin(websocket: WebSocket, settings: WebSettings | None = None) -> bool:
    """公网模式核对显式网址，不依赖反向代理是否转发了 https scheme。"""
    if settings and settings.public:
        origin = canonical_origin(websocket.headers.get("origin", ""))
        host_origin = canonical_origin(urlsplit(settings.public_url).scheme + "://" + websocket.headers.get("host", ""))
        return origin == settings.public_url and host_origin == settings.public_url
    try:
        origin = urlsplit(websocket.headers.get("origin", ""))
        expected_scheme = "https" if websocket.url.scheme == "wss" else "http"
        expected_port = websocket.url.port or (443 if expected_scheme == "https" else 80)
        origin_port = origin.port or (443 if origin.scheme == "https" else 80)
        return (
            websocket.url.hostname in {"localhost", "127.0.0.1"}
            and origin.scheme == expected_scheme
            and origin.hostname == websocket.url.hostname
            and origin_port == expected_port
            and not origin.username and not origin.password
            and origin.path in {"", "/"} and not origin.query and not origin.fragment
        )
    except ValueError:
        return False


def redact(value: Any) -> Any:
    """密钥留在服务端；日志或上游错误即使回显密钥也不会发送给浏览器。"""
    if isinstance(value, str):
        for name in ("OPENAI_API_KEY", "EMBEDDING_KEY", "WEB_ACCESS_PASSWORD"):
            secret = os.getenv(name, "").strip()
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def compact_result(value: Any, depth: int = 0) -> Any:
    """工具可能返回整份文件或图片，活动面板只传有限长度的预览。"""
    if depth > 5:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= 10_000 else value[:10_000] + "\n…（预览已截断）"
    if isinstance(value, dict):
        return {key: compact_result(item, depth + 1) for key, item in list(value.items())[:50]}
    if isinstance(value, list):
        return [compact_result(item, depth + 1) for item in value[:50]]
    return value


class BrowserSession:
    """接收协程只递交指令；worker 始终在同一个 Task 内管理 MCP 生命周期。"""

    def __init__(self, websocket: WebSocket, workspace: Path | None = None) -> None:
        self.websocket = websocket
        self.workspace = workspace
        self.commands: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=1)
        self.cancel_requested = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.busy = False
        self.disconnected = False
        self.agent: Agent | None = None
        self.retriever = None
        self.pending_answer: asyncio.Future[str] | None = None
        self.question_id: str | None = None

    async def send(self, event: dict[str, Any]) -> None:
        if self.disconnected:
            raise SessionExit
        async with self.send_lock:
            try:
                await self.websocket.send_json(redact(event))
            except (WebSocketDisconnect, RuntimeError, OSError):
                self.disconnected = True
                raise SessionExit from None

    async def status(self, state: str, message: str = "") -> None:
        await self.send({"type": "status", "state": state, "message": message})

    def checkpoint(self) -> None:
        if self.disconnected:
            raise SessionExit
        if self.cancel_requested.is_set():
            raise TaskCancelled

    async def on_event(self, event: dict[str, Any]) -> None:
        # 已完成的操作必须先回传真实结果，再响应取消，避免掩盖已发生的文件副作用。
        if event.get("type") == "tool_result":
            await self.send(compact_result(event))
            self.checkpoint()
            return
        self.checkpoint()
        if event.get("type") == "tool_start":
            await self.status("working", "正在执行工具")
        elif event.get("type") == "response_start":
            await self.status("thinking", "正在组织回答")
        if event.get("type") in {"tool_start", "tool_result", "tool_deferred"}:
            event = compact_result(event)
        await self.send(event)

    async def ask_user(self, question: str) -> str:
        """网页回调取代终端 input；得到答复后仍回到原来的 invoke 调用。"""
        self.checkpoint()
        self.question_id = uuid4().hex
        self.pending_answer = asyncio.get_running_loop().create_future()
        pending = self.pending_answer
        try:
            await self.status("waiting", "需要你补充一点信息")
            self.checkpoint()
            await self.send({"type": "question", "request_id": self.question_id, "question": question})
            answer = await pending
            self.checkpoint()
            await self.status("thinking", "收到回复，继续处理")
            return answer
        finally:
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                # 取消可能在问题发送之前到达；取出异常，避免留下未消费的 Future 错误。
                pending.exception()
            self.pending_answer = None
            self.question_id = None

    async def accept_command(self, command: Any) -> None:
        if not isinstance(command, dict):
            await self.send({"type": "error", "code": "invalid_message", "message": "消息格式不正确。"})
            return
        kind = command.get("type")
        # 【修复】先校验类型，避免数组或对象在后续集合判断中抛异常并断开连接。
        if not isinstance(kind, str):
            await self.send({"type": "error", "code": "invalid_message", "message": "消息 type 必须是字符串。"})
            return
        if kind in {"message", "reset"}:
            if self.busy:
                await self.send({"type": "error", "code": "busy", "message": "当前任务仍在进行，请先回复问题或取消任务。"})
                return
            content = command.get("content", "")
            if kind == "message" and (not isinstance(content, str) or not content.strip()):
                await self.send({"type": "error", "code": "invalid_message", "message": "请输入任务内容。"})
                return
            self.busy = True
            self.commands.put_nowait((kind, content.strip() if kind == "message" else ""))
        elif kind == "answer":
            if (
                self.pending_answer is None or self.pending_answer.done()
                or command.get("request_id") != self.question_id
            ):
                await self.send({"type": "error", "code": "stale_question", "message": "这个问题已经结束，请等待当前任务状态更新。"})
                return
            answer = command.get("content")
            if not isinstance(answer, str) or not answer.strip():
                await self.send({"type": "error", "code": "invalid_answer", "message": "请输入你的回复。"})
                return
            await self.send({"type": "answer_accepted", "request_id": self.question_id, "content": answer.strip()})
            self.pending_answer.set_result(answer.strip())
        elif kind == "cancel":
            if not self.busy:
                await self.status("idle")
                return
            self.cancel_requested.set()
            await self.status("cancelling", "正在停止当前任务，已执行的操作不会撤销")
            if self.pending_answer is not None and not self.pending_answer.done():
                self.pending_answer.set_exception(TaskCancelled())
        else:
            await self.send({"type": "error", "code": "invalid_message", "message": "不支持的操作。"})

    async def close_agent(self) -> None:
        if self.agent is not None:
            # 不创建子 Task、不使用 wait_for：stdio 的取消作用域必须在创建它的 Task 中退出。
            await self.agent.close()
            self.agent = None

    async def run_turn(self, content: str) -> None:
        try:
            self.checkpoint()
            if self.retriever is None:
                await self.status("initializing", "正在准备知识库和工具")
                self.retriever = await create_retriever()
                self.checkpoint()
            if self.agent is None:
                if self.workspace is not None:
                    self.workspace.mkdir(parents=True, exist_ok=True)
                    clients = create_mcp_clients(file_root=self.workspace, allow_private_ips=False)
                else:
                    clients = create_mcp_clients()
                options = {"ask_user_callback": self.ask_user, "event_callback": self.on_event}
                if self.workspace is not None:
                    options["system_prompt"] = (
                        f"当前用户的文件工作目录是 {self.workspace}。所有文件读取和写入都应在此目录内。"
                        "保存成果时使用此目录，用户可以通过页面的‘我的文件’下载。"
                        "这些是服务器上的文件，不能声称已修改用户电脑上的本地文件。"
                    )
                # 【B2】终版阶段二次检索：用「首问 + 本轮追问问答」再查一次知识库。
                # 复用本会话的 retriever 与检索函数，口径与 eval 的 finalize_context 一致；
                # reset 时 agent 与 retriever 一起重建，闭包引用的 self.retriever 始终是当前实例。
                async def finalize_context(query: str) -> str:
                    return await retrieve_context(query, retriever=self.retriever)

                options["finalize_context"] = finalize_context
                self.agent = Agent(os.getenv("OPENAI_MODEL", "deepseek-v4-pro"), clients, **options)
            if self.agent.llm is None:
                await self.agent.init()
                self.checkpoint()
            await self.status("retrieving", "正在查找相关资料")
            context = await retrieve_context(content, retriever=self.retriever)
            self.checkpoint()
            await self.status("thinking", "正在处理你的任务")
            response = await self.agent.invoke(content, context=context)
            self.checkpoint()
            await self.send({"type": "done", "content": response})
        except TaskCancelled:
            await self.send({"type": "cancelled"})
        except SessionExit:
            raise
        except Exception as exc:
            await self.send({"type": "error", "code": "task_error", "message": str(exc)})
        finally:
            self.cancel_requested.clear()
            self.busy = False
            if not self.disconnected:
                await self.status("idle")

    async def run(self) -> None:
        try:
            while True:
                kind, content = await self.commands.get()
                # 接收协程可能在 worker 开始前收到取消，不能在这里清掉该标记。
                if kind == "reset":
                    try:
                        await self.close_agent()
                        self.retriever = None
                        await self.send({"type": "reset"})
                    except Exception as exc:
                        await self.send({"type": "error", "code": "reset_error", "message": str(exc)})
                    finally:
                        self.cancel_requested.clear()
                        self.busy = False
                        await self.status("idle")
                else:
                    await self.run_turn(content)
        finally:
            if self.pending_answer is not None and not self.pending_answer.done():
                self.pending_answer.cancel()
            await self.close_agent()


async def chat_socket(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    store = websocket.app.state.logins
    if not same_origin(websocket, settings):
        await websocket.close(code=1008, reason="仅允许已配置的同源页面连接")
        return
    login = store.get(websocket.cookies.get(COOKIE_NAME)) if settings.public else None
    if settings.public and login is None:
        # 握手成功后发送登录关闭码，浏览器才能识别 4401 并跳回登录页。
        await websocket.accept()
        await websocket.close(code=4401, reason="请先登录工作空间")
        return
    connections = websocket.app.state.connections
    if settings.public and len(connections) >= settings.max_connections:
        await websocket.accept()
        await websocket.close(code=1013, reason="当前使用人数已达到上限，请稍后再试")
        return
    connections.add(websocket)
    try:
        await websocket.accept()
    except BaseException:
        connections.discard(websocket)
        raise
    if login:
        login.sockets.add(websocket)
    session = BrowserSession(websocket, store.workspace(login) if login else None)
    worker = asyncio.create_task(session.run(), name="browser-agent-session")
    try:
        ready = {"type": "ready"}
        if settings.public:
            ready.update({"authenticated": True, "files_url": "/files"})
        await session.send(ready)
        await session.status("idle")
        while True:
            if login:
                if store.get(websocket.cookies.get(COOKIE_NAME)) is not login:
                    await websocket.close(code=4401, reason="登录已过期，请重新登录")
                    break
                try:
                    frame = await asyncio.wait_for(websocket.receive(), timeout=max(.01, login.expires_at - time.time()))
                except TimeoutError:
                    await websocket.close(code=4401, reason="登录已过期，请重新登录")
                    break
            else:
                frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                break
            text = frame.get("text")
            if not isinstance(text, str) or len(text) > MAX_MESSAGE_CHARS:
                await session.send({"type": "error", "code": "invalid_message", "message": "请发送文字消息，每条不超过 50,000 个字符。"})
                continue
            try:
                command = json.loads(text)
            except (ValueError, RecursionError):
                await session.send({"type": "error", "code": "invalid_message", "message": "消息不是有效的 JSON。"})
                continue
            await session.accept_command(command)
    except (WebSocketDisconnect, SessionExit):
        pass
    finally:
        connections.discard(websocket)
        if login:
            login.sockets.discard(websocket)
        session.disconnected = True
        # 请求/浏览器关闭可能同时取消外层 AnyIO 作用域。只保护这里的等待，
        # 不在持有 MCP 的 worker 内插入新作用域，保证它仍能按原顺序完整退出。
        with anyio.CancelScope(shield=True):
            worker.cancel()
            with suppress(asyncio.CancelledError, SessionExit):
                await worker


def create_app(web_directory: Path | None = None, *, settings: WebSettings | None = None) -> Starlette:
    directory = web_directory or PROJECT_ROOT / "web"
    settings = settings or WebSettings.from_env()
    logins = LoginStore(settings)
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"}

    def logged_in(request):
        return logins.get(request.cookies.get(COOKIE_NAME))

    def form_origin_ok(request):
        return canonical_origin(request.headers.get("origin", "")) == settings.public_url

    def login_page(message="", status=200):
        content = (directory / "login.html").read_text(encoding="utf-8")
        content = content.replace("<!-- LOGIN_ERROR -->", f'<p class="login-error" role="alert">{escape(message)}</p>' if message else "")
        return HTMLResponse(content, status_code=status, headers=headers)

    async def homepage(request):
        if settings.public and not logged_in(request):
            return RedirectResponse("/auth/login", status_code=303, headers=headers)
        return FileResponse(directory / "index.html", headers=headers)

    async def login(request):
        if not settings.public:
            return RedirectResponse("/", status_code=303)
        if request.method == "GET":
            return RedirectResponse("/", status_code=303, headers=headers) if logged_in(request) else login_page()
        if not form_origin_ok(request):
            return HTMLResponse("登录请求来源不正确，请从工作空间登录页重试。", status_code=403, headers=headers)
        address = request.client.host if request.client else "unknown"
        retry = logins.retry_after(address)
        if retry:
            response = login_page("尝试次数较多，请稍等一分钟再试。", 429)
            response.headers["Retry-After"] = str(retry)
            return response
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                return login_page("输入过长，请重新输入访问口令。", 413)
            body.extend(chunk)
        try:
            form = parse_qs(body.decode("utf-8"), max_num_fields=4, errors="strict")
            password = form.get("password", [""])[0]
        except (ValueError, UnicodeError):
            return login_page("无法读取登录信息，请重试。", 400)
        if not secrets.compare_digest(password.encode("utf-8"), settings.access_password.encode("utf-8")):
            logins.record_failure(address)
            return login_page("访问口令不正确，请再试一次。", 401)
        try:
            token, _ = logins.issue()
        except RuntimeError:
            return login_page("当前使用人数较多，请稍后重试。", 503)
        logins.failures.pop(address, None)
        response = RedirectResponse("/", status_code=303, headers=headers)
        response.set_cookie(COOKIE_NAME, token, max_age=settings.session_seconds, httponly=True,
                            secure=settings.secure_cookie, samesite="strict", path="/")
        return response

    async def logout(request):
        if not settings.public or not form_origin_ok(request):
            return HTMLResponse("请求来源不正确。", status_code=403, headers=headers)
        session = logins.revoke(request.cookies.get(COOKIE_NAME))
        if session:
            for socket in list(session.sockets):
                with suppress(RuntimeError, WebSocketDisconnect):
                    await socket.close(code=4401, reason="已退出登录")
        response = RedirectResponse("/auth/login", status_code=303, headers=headers)
        response.delete_cookie(COOKIE_NAME, path="/", secure=settings.secure_cookie, httponly=True, samesite="strict")
        return response

    async def files(request):
        if not settings.public:
            return HTMLResponse("Not found", status_code=404)
        session = logged_in(request)
        if not session:
            return RedirectResponse("/auth/login", status_code=303, headers=headers)
        root = logins.workspace(session).resolve()
        relative = request.path_params.get("path", "")
        if relative:
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return HTMLResponse("文件不存在或不在当前工作空间内。", status_code=404, headers=headers)
            return FileResponse(target, media_type="application/octet-stream", filename=target.name,
                                content_disposition_type="attachment", headers=headers)
        entries = []
        if root.is_dir():
            for folder, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
                for name in sorted(names):
                    path = Path(folder) / name
                    if path.is_symlink():
                        continue
                    rel = path.relative_to(root).as_posix()
                    entries.append(f'<li><a href="/files/{quote(rel, safe="/")}">{escape(rel)}</a><span>下载 ↓</span></li>')
                    if len(entries) >= 200:
                        break
                if len(entries) >= 200:
                    break
        content = (directory / "files.html").read_text(encoding="utf-8")
        content = content.replace("<!-- WORKSPACE_FILES -->", "".join(entries) or '<li class="empty">还没有文件。请先让 Mellow 将成果保存到文件。</li>')
        return HTMLResponse(content, headers=headers)

    async def health(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/", homepage), Route("/health", health),
            Route("/auth/login", login, methods=["GET", "POST"]),
            Route("/auth/logout", logout, methods=["POST"]),
            Route("/files", files), Route("/files/{path:path}", files),
            WebSocketRoute("/ws", chat_socket),
            Mount("/static", app=StaticFiles(directory=directory, check_dir=False), name="static"),
        ],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts, www_redirect=False)],
    )
    app.state.settings = settings
    app.state.logins = logins
    app.state.connections = set()
    return app


app = create_app()
