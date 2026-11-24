"""公网部署配置与短期登录会话；本地模式默认不启用登录。"""

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from .config import PROJECT_ROOT

COOKIE_NAME = "mellow_session"


def canonical_origin(value: str) -> str:
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname:
            return ""
        if url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
            return ""
        port = url.port or (443 if url.scheme == "https" else 80)
        host = url.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        suffix = "" if port == (443 if url.scheme == "https" else 80) else f":{port}"
        return f"{url.scheme}://{host}{suffix}"
    except ValueError:
        return ""


@dataclass(frozen=True)
class WebSettings:
    public_url: str = ""
    access_password: str = field(default="", repr=False)
    workspace_root: Path = PROJECT_ROOT / ".web-workspaces"
    max_connections: int = 4
    session_seconds: int = 12 * 60 * 60

    def __post_init__(self):
        if not 1 <= self.max_connections <= 100:
            raise ValueError("WEB_MAX_CONNECTIONS 必须在 1 到 100 之间")
        if not 60 <= self.session_seconds <= 7 * 24 * 60 * 60:
            raise ValueError("WEB_SESSION_SECONDS 必须在 60 到 604800 之间")
        if self.public_url:
            origin = canonical_origin(self.public_url)
            if not origin:
                raise ValueError("WEB_PUBLIC_URL 必须是完整的网址，且不能包含路径、查询或账号")
            url = urlsplit(origin)
            if url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1"}:
                raise ValueError("公网 WEB_PUBLIC_URL 必须使用 https")
            if not 12 <= len(self.access_password) <= 256:
                raise ValueError("公网部署需要 12 到 256 个字符的 WEB_ACCESS_PASSWORD")
            object.__setattr__(self, "public_url", origin)
        elif self.access_password:
            raise ValueError("配置访问口令时也必须设置 WEB_PUBLIC_URL")

    @classmethod
    def from_env(cls):
        return cls(
            public_url=os.getenv("WEB_PUBLIC_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip(),
            access_password=os.getenv("WEB_ACCESS_PASSWORD", ""),
            workspace_root=Path(os.getenv("WEB_WORKSPACE_ROOT") or PROJECT_ROOT / ".web-workspaces").resolve(),
            max_connections=int(os.getenv("WEB_MAX_CONNECTIONS", "4")),
            session_seconds=int(os.getenv("WEB_SESSION_SECONDS", str(12 * 60 * 60))),
        )

    @property
    def public(self):
        return bool(self.public_url)

    @property
    def secure_cookie(self):
        return self.public_url.startswith("https://")

    @property
    def allowed_hosts(self):
        return list(dict.fromkeys(["localhost", "127.0.0.1"] + ([urlsplit(self.public_url).hostname] if self.public else [])))


@dataclass
class LoginSession:
    workspace_id: str
    expires_at: float
    sockets: set = field(default_factory=set)


class LoginStore:
    """不把口令放入 cookie；重启后登录失效，重新登录会得到新的文件目录。"""

    def __init__(self, settings: WebSettings):
        self.settings = settings
        self.sessions: dict[str, LoginSession] = {}
        self.failures: dict[str, list[float]] = {}

    @staticmethod
    def key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def get(self, token: str | None) -> LoginSession | None:
        if not token or len(token) > 100:
            return None
        key = self.key(token)
        session = self.sessions.get(key)
        if session and session.expires_at > time.time():
            return session
        self.sessions.pop(key, None)
        return None

    def issue(self) -> tuple[str, LoginSession]:
        now = time.time()
        self.sessions = {key: item for key, item in self.sessions.items() if item.expires_at > now}
        if len(self.sessions) >= 1000:
            raise RuntimeError("当前登录人数已达到上限，请稍后重试")
        token = secrets.token_urlsafe(32)
        session = LoginSession(secrets.token_hex(16), now + self.settings.session_seconds)
        self.sessions[self.key(token)] = session
        return token, session

    def revoke(self, token: str | None) -> LoginSession | None:
        return self.sessions.pop(self.key(token), None) if token else None

    def retry_after(self, address: str) -> int:
        now = time.time()
        self.failures = {key: [stamp for stamp in stamps if stamp > now - 60]
                         for key, stamps in self.failures.items() if stamps and stamps[-1] > now - 60}
        stamps = self.failures.get(address, [])
        return max(1, int(60 - (now - stamps[0])) + 1) if len(stamps) >= 5 else 0

    def record_failure(self, address: str):
        # 限制状态大小，即使客户端大量改变地址也不会无限积累。
        if len(self.failures) >= 1024 and address not in self.failures:
            self.failures.pop(next(iter(self.failures)))
        self.failures.setdefault(address, []).append(time.time())

    def workspace(self, session: LoginSession) -> Path:
        return self.settings.workspace_root / session.workspace_id
