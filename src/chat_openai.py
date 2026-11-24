"""流式接收模型文字和工具调用，按 index 拼接工具调用片段。"""

from dataclasses import asdict, dataclass, field
import os
from typing import Any

from openai import AsyncOpenAI

from .config import required_env
from .events import EventCallback, emit_event
from .utils import log_title


@dataclass
class FunctionCall:
    name: str = ""
    arguments: str = ""


@dataclass
class ToolCall:
    id: str = ""
    function: FunctionCall = field(default_factory=FunctionCall)

    def to_message(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function", "function": asdict(self.function)}


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall]


class ChatOpenAI:
    def __init__(
        self,
        model: str,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        context: str = "",
        *, event_callback: EventCallback | None = None,
    ) -> None:
        self.model = model
        self.tools = tools or []
        self.event_callback = event_callback
        self.messages: list[dict[str, Any]] = []
        # 【评估可复现】设置了 EVAL_SEED 时才固定随机源；产品正常运行不设此变量，行为不变。
        self.eval_seed = os.getenv("EVAL_SEED", "").strip()
        self.llm = AsyncOpenAI(
            api_key=required_env("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
        )
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})
        if context:
            self.messages.append({"role": "user", "content": context})

    # 【修改】复用 self.messages 保留多轮历史，并为当前问题附上本轮检索资料。
    async def chat(
        self, prompt: str | None = None, context: str | None = None,
        *, allow_tools: bool = True,
    ) -> ChatResponse:
        log_title("CHAT")
        if prompt:
            content = prompt
            if context:
                content = f"参考资料（用于回答本轮问题）：\n{context}\n\n用户问题：\n{prompt}"
            self.messages.append({"role": "user", "content": content})
        request: dict[str, Any] = {
            "model": self.model, "messages": self.messages, "stream": True,
        }
        if self.eval_seed:
            request["seed"] = int(self.eval_seed)
            request["temperature"] = 0
        # 【修改】只控制本次请求；B2 终版不提供工具，下一轮仍正常使用工具。
        if allow_tools and self.tools:
            request["tools"] = self.get_tools_definition()
        # 【新增】界面可以按每次模型响应分段显示，而不依赖终端 stdout。
        await emit_event(self.event_callback, "response_start")
        stream = await self.llm.chat.completions.create(**request)
        content_parts: list[str] = []
        calls: dict[int, ToolCall] = {}
        log_title("RESPONSE")
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    content_parts.append(delta.content)
                    await emit_event(self.event_callback, "delta", content=delta.content)
                    print(delta.content, end="", flush=True)
                for part in delta.tool_calls or []:
                    call = calls.setdefault(part.index, ToolCall())
                    if part.id:
                        call.id += part.id
                    if part.function:
                        call.function.name += part.function.name or ""
                        call.function.arguments += part.function.arguments or ""
        finally:
            await stream.close()
        content = "".join(content_parts)
        if content:
            print()
        tool_calls = [calls[index] for index in sorted(calls)]
        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if tool_calls:
            message["tool_calls"] = [call.to_message() for call in tool_calls]
        self.messages.append(message)
        return ChatResponse(content, tool_calls)

    def append_tool_result(self, tool_call_id: str, tool_output: str) -> None:
        self.messages.append({
            "role": "tool", "content": tool_output, "tool_call_id": tool_call_id,
        })

    def get_tools_definition(self) -> list[dict[str, Any]]:
        return [{
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description") or "",
                "parameters": tool["inputSchema"],
            },
        } for tool in self.tools]

    async def close(self) -> None:
        await self.llm.close()
