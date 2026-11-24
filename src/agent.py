"""手写 Agent 循环，不使用 LangChain 或其他 Agent 框架。"""

from collections.abc import Awaitable, Callable
from copy import deepcopy
import json

from .chat_openai import ChatOpenAI
from .context_documents import merge_contexts
from .events import EventCallback, emit_event
from .mcp_client import MCPClient
from .utils import log_title


# 【新增】入口负责实现交互方式，Agent 只接收异步的“问题 -> 用户答复”函数。
AskUserCallback = Callable[[str], Awaitable[str]]

# 【B2】最终渲染前，用“首问 + 追问问答”重新检索知识库的函数类型。
# 输入是合成后的检索 query，输出是新检索到的参考资料文本。
FinalizeContext = Callable[[str], Awaitable[str]]

# 【修改】追加到原会话的终版指令；工具由本次请求的 allow_tools=False 关闭。
FINALIZE_INSTRUCTION = """当前任务已进入最终输出阶段。请结合会话中用户已经确认的需求、已有工具结果和补充参考资料，直接给出最终答案。
要求：
1. 直接输出最终结果，不再向用户提问，不请求补充信息，不再调用工具。仍不确定的条件应明确说明假设或局限。
2. 用户明确提出的需求（含追问中确认的）要逐条满足。
3. 涉及门票、开放时间、价格、交通等事实，以参考资料和实际工具结果为依据；没有依据的，不要编造，可注明需要核实。
4. 资料中的日期、天数、同行人、偏好不代表用户的需求；按用户最新明确要求修改，继续沿用其余相关条件。"""

ASK_USER_TOOL = {
    "name": "ask_user",
    "description": "需要用户补充关键信息、明确关键意图或确认授权时，暂停任务并等待用户答复。",
    "inputSchema": {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "结合当前任务向用户提出的问题"}},
        "required": ["question"],
        "additionalProperties": False,
    },
}

# 【修改】在原人机交互提示词后追加 RAG 资料使用与需求澄清规则。
HUMAN_INTERACTION_PROMPT = """你可以在执行任务的过程中调用 ask_user 向用户询问。
请自主决定是否需要询问：常规细节自行处理；只有缺少会实质影响结果且无法合理推断的关键信息、
关键意图或操作授权不明确时，才询问用户。用户已经明确提供或授权的事项不要重复确认。
问题由你根据当前任务、历史和工具结果生成，可以不问，也可以在后续发现新问题时再次询问。
需要用户回答时必须调用 ask_user，不要只在普通回答里提出问题就结束任务。
等待答复时不要同批调用其他工具；ask_user 的工具结果包含用户的真实回复，收到后继续原任务。
如果其他工具返回 deferred，表示该操作尚未执行，请结合用户答复重新决定是否调用。
可用的网页和文件操作能力以提供的 MCP 工具为准；只有工具实际执行成功，才能声称操作完成。

使用知识库参考资料时，请区分“资料充分”和“用户需求明确”：

1. 参考资料提供的是外部事实、候选方案和他人经验。资料中的日期、预算、天数、同行人及偏好，不代表当前用户已经确认的条件，不能直接套用。

2. 当用户要求定制规划、制定策略或执行任务时，先结合当前问题和对话历史，判断是否存在会实质改变方案、且需要用户本人确认的信息缺口。即使参考资料已经给出完整攻略，也要独立进行这一判断。

3. 如果存在上述关键缺口，先调用 ask_user，等待用户答复，再继续制定方案。可以简短说明背景或提供选项帮助用户回答，但不要先输出依赖未确认条件的完整方案，再在结尾追问。普通回答中的文字提问不能代替 ask_user。

4. 根据当前任务自主决定询问内容和顺序，不使用固定问卷。每次优先询问最影响下一步决策的少量问题；收到答复后重新判断，可以继续询问，也可以开始规划。

5. 已经明确的信息不要重复询问。用户只需要通用介绍、明确要求先给初稿、允许采用默认条件，或缺少的细节不影响主要结论时，可以直接回答，并在必要时说明假设。

6. 用户答复后，以已确认的需求为依据筛选和调整参考方案。参考资料与用户条件不匹配时，应调整方案，不能为了套用攻略而忽略用户要求。
"""


class TaskCancelled(Exception):
    """【新增】取消当前任务，保留整场对话和之前成功的历史。"""


class SessionExit(Exception):
    """【新增】结束整场对话，由入口统一清理模型和 MCP 连接。"""


class Agent:
    def __init__(
        self, model: str, mcp_clients: list[MCPClient],
        system_prompt: str = "", context: str = "", max_rounds: int = 50,
        *, ask_user_callback: AskUserCallback | None = None,
        event_callback: EventCallback | None = None,
        finalize_context: FinalizeContext | None = None,
    ) -> None:
        self.model = model
        self.mcp_clients = mcp_clients
        self.system_prompt = system_prompt
        self.context = context
        self.max_rounds = max_rounds
        self.ask_user_callback = ask_user_callback
        self.event_callback = event_callback
        # 【修改】传了 finalize_context 才启用重检索+原会话终版；A/B1 不传，行为不变。
        self.finalize_context = finalize_context
        self.last_draft: str | None = None  # B2 里被终版替换掉的草稿，供评估流程留档
        self.llm: ChatOpenAI | None = None
        self._opened: list[MCPClient] = []

    async def init(self) -> None:
        if self.llm is not None:
            return
        log_title("TOOLS")
        try:
            for client in self.mcp_clients:
                await client.init()
                self._opened.append(client)
            tools = [tool for client in self.mcp_clients for tool in client.get_tools()]
            # 【新增】有交互入口时，将本地询问工具与原有 MCP 工具一起提供给模型。
            system_prompt = self.system_prompt
            if self.ask_user_callback is not None:
                tools.append(ASK_USER_TOOL)
                system_prompt = "\n\n".join(filter(None, [system_prompt, HUMAN_INTERACTION_PROMPT]))
            names = [tool["name"] for tool in tools]
            if len(names) != len(set(names)):
                raise ValueError("存在同名工具（包括 MCP 和本地工具），无法确定调用目标")
            if self.event_callback is None:
                self.llm = ChatOpenAI(self.model, system_prompt, tools, self.context)
            else:
                self.llm = ChatOpenAI(
                    self.model, system_prompt, tools, self.context,
                    event_callback=self.event_callback,
                )
            await emit_event(self.event_callback, "tools", tools=[
                {"name": tool["name"], "description": tool.get("description") or ""}
                for tool in tools
            ])
        except BaseException:
            await self.close()
            raise

    # 【新增】本地工具：通过保存的回调获取答复，不与终端输入方式绑定。
    async def ask_user(self, question: str) -> dict[str, str]:
        if self.ask_user_callback is None:
            raise RuntimeError("未配置用户交互回调")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question 必须是非空字符串")
        answer = await self.ask_user_callback(question)
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("用户答复必须是非空字符串")
        return {"answer": answer}

    # 【新增】界面与终端复用同一套工具执行，交互方式由回调决定。
    async def _execute_tool(self, name: str, params: dict) -> dict:
        if self.ask_user_callback is not None and name == "ask_user":
            return await self.ask_user(**params)
        client = next((
            c for c in self.mcp_clients
            if any(tool["name"] == name for tool in c.get_tools())
        ), None)
        if client is None:
            raise ValueError(f"Tool not found: {name}")
        return await client.call_tool(name, params)

    def _collect_ask_qa(self) -> list[tuple[str, str]]:
        """从本轮对话历史里还原 ask_user 的问答对（按发生顺序）。"""
        if self.llm is None:
            return []
        answers: dict[str, str] = {}
        for msg in self.llm.messages:
            if msg.get("role") == "tool":
                try:
                    data = json.loads(msg.get("content") or "{}")
                except Exception:
                    continue
                if isinstance(data, dict) and "answer" in data:
                    answers[msg.get("tool_call_id")] = str(data["answer"])
        pairs: list[tuple[str, str]] = []
        for msg in self.llm.messages:
            if msg.get("role") != "assistant":
                continue
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                if fn.get("name") != "ask_user":
                    continue
                try:
                    params = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    continue
                question = params.get("question") if isinstance(params, dict) else None
                cid = call.get("id")
                if question is not None and cid in answers:
                    pairs.append((str(question), answers[cid]))
        return pairs

    @staticmethod
    def _merge_contexts(first: str, second: str) -> str:
        """【修复】与裁判共用整篇去重，保留不同景点相同的事实行。"""
        return merge_contexts(first or "", second or "")

    def _build_finalize_content(self, merged_context: str) -> str:
        """【修改】仅追加参考资料和阶段指令，需求与问答仍从原历史读取。"""
        return (
            f"【补充参考资料（首轮与二次检索合并，整篇去重）】\n{merged_context}\n\n"
            f"【本次输出要求】\n{FINALIZE_INSTRUCTION}"
        )

    async def render_final(
        self, prompt: str, qa: list[tuple[str, str]], draft: str,
        first_context: str, finalize_context: FinalizeContext,
    ) -> tuple[str, str, bool]:
        """【修改】原会话去掉末条草稿，补资料，单次禁工具并正常记录终版。

        返回 (终版文本, 合并资料, 是否成功生成终版)。失败或空输出恢复原历史。
        """
        assert self.llm is not None
        query_parts = [prompt]
        query_parts += [f"用户追加问题：{q}\n用户回答：{a}" for q, a in qa]
        new_context = await finalize_context("\n".join(query_parts))
        merged_context = self._merge_contexts(first_context, new_context)
        self.last_draft = draft
        if not merged_context.strip():
            return draft, merged_context, False  # 两轮都空，保留草稿，避免空壳终版
        messages = self.llm.messages
        if not messages or (
            messages[-1].get("role") != "assistant"
            or messages[-1].get("tool_calls")
            or (messages[-1].get("content") or "") != draft
        ):
            raise ValueError("B2 需要以本轮完整草稿结尾的对话历史；评测请先重新运行 B。")
        original = messages[:]
        try:
            messages.pop()  # 只排除本轮最后的完整草稿，保留其余消息及工具配对。
            response = await self.llm.chat(
                self._build_finalize_content(merged_context), allow_tools=False,
            )
            if response.tool_calls:
                raise RuntimeError("终版阶段不允许工具调用，未执行任何返回的工具请求。")
            if not response.content.strip():
                messages[:] = original
                return draft, merged_context, False
        except BaseException:
            messages[:] = original
            raise
        return response.content, merged_context, True

    async def finalize_from_record(
        self, prompt: str, qa: list[tuple[str, str]], draft: str,
        first_context: str, finalize_context: FinalizeContext,
        *, messages: list[dict] | None = None,
    ) -> str:
        """【修改】eval 恢复 B 的完整历史，与产品使用同一终版流程。"""
        if not isinstance(messages, list) or not messages or not all(isinstance(msg, dict) for msg in messages):
            raise ValueError("B2 需要 B 保存的完整 messages，请先重新运行 B。")
        assert self.llm is not None
        self.llm.messages[:] = deepcopy(messages)
        final, _merged, _rendered = await self.render_final(
            prompt, qa, draft, first_context, finalize_context
        )
        return final

    async def _finalize(self, prompt: str, draft: str, first_context: str = "") -> str:
        """【修改】重检索后沿原会话生成终版；chat 正常入历史，无额外回填。"""
        assert self.llm is not None and self.finalize_context is not None

        qa = self._collect_ask_qa()
        final, _merged_context, _rendered = await self.render_final(
            prompt, qa, draft, first_context, self.finalize_context
        )
        return final

    async def _finish(self, prompt: str, draft: str, first_context: str | None = None) -> str:
        """A/B1 直接返回草稿；只有 B2 才补资料并在原会话输出终版。"""
        if self.finalize_context is None:
            return draft
        return await self._finalize(prompt, draft, first_context or "")

    # 【修改】一轮提问结束后保留模型与 MCP 连接，由会话入口统一关闭。
    async def invoke(self, prompt: str, context: str | None = None) -> str:
        if self.llm is None:
            raise RuntimeError("Agent 尚未初始化，请先 await agent.init()")
        # 【新增】失败时撤回本轮消息，避免未完成的工具调用破坏下一轮请求。
        history_size = len(self.llm.messages)
        try:
            response = await self.llm.chat(prompt, context=context)
            for _ in range(self.max_rounds):
                if not response.tool_calls:
                    return await self._finish(prompt, response.content, context)
                # 【新增】先检查整批，避免排在询问前面的其他工具被提前执行。
                has_question = self.ask_user_callback is not None and any(
                    call.function.name == "ask_user" for call in response.tool_calls
                )
                for call in response.tool_calls:
                    log_title("TOOL USE")
                    print(f"Calling tool: {call.function.name}")
                    print(f"Arguments: {call.function.arguments}")
                    # 【修改】询问批次只执行 ask_user，其他工具回填“未执行”状态。
                    if has_question and call.function.name != "ask_user":
                        result = {
                            "deferred": True, "executed": False,
                            "reason": "本批包含用户询问，请结合用户答复重新决定是否调用此工具。",
                        }
                        event_kind = "tool_deferred"
                    else:
                        event_kind = "tool_result"
                        try:
                            params = json.loads(call.function.arguments or "{}")
                            if not isinstance(params, dict):
                                raise ValueError("工具参数必须是 JSON 对象")
                        except Exception as exc:
                            result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
                        else:
                            # 【新增】事件回调位于工具错误包装之外，网页取消可直接中断当前轮。
                            await emit_event(
                                self.event_callback, "tool_start",
                                id=call.id, name=call.function.name, arguments=params,
                            )
                            try:
                                result = await self._execute_tool(call.function.name, params)
                            except (TaskCancelled, SessionExit):
                                raise
                            except Exception as exc:
                                result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
                    output = json.dumps(result, ensure_ascii=False)
                    print(f"Result: {output}")
                    self.llm.append_tool_result(call.id, output)
                    await emit_event(
                        self.event_callback, event_kind,
                        id=call.id, name=call.function.name, result=result,
                    )
                response = await self.llm.chat()
            if not response.tool_calls:
                return await self._finish(prompt, response.content, context)
            raise RuntimeError(f"工具调用已达到 {self.max_rounds} 轮，任务停止")
        except Exception:
            del self.llm.messages[history_size:]
            raise
        # 【删除说明】删除每轮 finally 中的 self.close()，改由调用方在整场会话结束时执行。

    async def close(self) -> None:
        if self.llm is None and not self._opened:
            return
        log_title("CLOSE")
        # stdio SDK 使用嵌套的任务/取消作用域，必须与初始化相反的顺序退出。
        try:
            if self.llm is not None:
                await self.llm.close()
        finally:
            self.llm = None
            while self._opened:
                await self._opened.pop().close()
