"""【新增】向界面回传事件；未配置回调时保留原来的终端行为。"""

from collections.abc import Awaitable, Callable
from typing import Any


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


async def emit_event(callback: EventCallback | None, kind: str, **payload: Any) -> None:
    if callback is not None:
        await callback({"type": kind, **payload})
