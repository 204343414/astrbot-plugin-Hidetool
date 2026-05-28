"""
AstrBot Plugin: Tool Gate (工具懒加载)

隐藏所有工具的完整 schema，LLM 视角只看到一个 activate_tools 元工具 + 精简目录。
LLM 调用 activate_tools 后，全部工具恢复原样，整个 tool loop 回合内保持解锁。
下一次用户消息重新锁定。

核心机制:
  1. on_llm_request hook 拦截 → 缓存真实工具 → 替换为 activate_tools
  2. activate_tools 被调用 → 往 req.func_tool.tools 里塞回所有真实工具
  3. tool loop 继续 → LLM 看到完整工具列表 → 正常调用
  4. tool loop 结束(LLM 给出文本回复) → 本回合结束
  5. 下一次用户消息 → on_llm_request 再次拦截 → 重新锁定
"""

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger, AstrBotConfig

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.agent.message import TextPart


@register(
    "astrbot_plugin_tool_gate",
    "Arena.ai Agent",
    "工具懒加载 - 隐藏工具描述节省 70%+ token",
    "0.1.0",
)
class ToolGatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._current_event: AstrMessageEvent | None = None

    # ──────────────────────────────────────────────
    #  核心 Hook: 在 LLM 请求前拦截工具列表
    # ──────────────────────────────────────────────
    @filter.on_llm_request(priority=-99)
    async def intercept_tools(self, event: AstrMessageEvent, req: ProviderRequest):
        self._current_event = event

        if not self.config.get("enabled", True):
            return

        func_tool: ToolSet | None = req.func_tool
        if func_tool is None or func_tool.empty():
            return

        all_tools: list[FunctionTool] = list(func_tool.tools)
        if not all_tools:
            return

        min_count = self.config.get("min_tools_to_activate", 3)
        if len(all_tools) < min_count:
            self._debug(
                f"[ToolGate] 工具数 {len(all_tools)} < 阈值 {min_count}，跳过"
            )
            return

        tool_names = [t.name for t in all_tools]
        if "activate_tools" in tool_names:
            return

        self._debug(f"[ToolGate] 🔒 拦截 {len(all_tools)} 个工具: {tool_names}")

        always_active_names = set(self.config.get("always_active_tools", []))
        hidden_tools: list[FunctionTool] = []
        kept_tools: list[FunctionTool] = []

        for tool in all_tools:
            if tool.name in always_active_names:
                kept_tools.append(tool)
            else:
                hidden_tools.append(tool)

        if not hidden_tools:
            self._debug("[ToolGate] 所有工具都在白名单中，跳过")
            return

        # ── 生成工具目录文本 ──
        catalog_lines = []
        for tool in hidden_tools:
            desc = (tool.description or "").split("\n")[0].strip()
            if len(desc) > 60:
                desc = desc[:57] + "..."
            catalog_lines.append(f"  • {tool.name}: {desc}")

        catalog_text = (
            "\n<tool_gate_system_notice>\n"
            "【系统】为节省资源，你的工具箱当前处于待命状态。\n"
            "下面是你拥有的全部工具能力一览：\n"
            + "\n".join(catalog_lines)
            + "\n\n"
            "当你判断需要调用任何工具时，请先调用 activate_tools 来解锁。\n"
            "解锁是系统层面的操作，不需要向用户解释这个过程。\n"
            "如果当前对话不需要任何工具，直接正常回复即可。\n"
            "</tool_gate_system_notice>"
        )

        req.extra_user_content_parts.append(TextPart(text=catalog_text).mark_as_temp())

        activate_tool = _make_activate_tool(
            hidden_tools=hidden_tools,
            target_tool_set=func_tool,
            debug_fn=self._debug,
        )

        func_tool.tools.clear()
        func_tool.add_tool(activate_tool)
        for t in kept_tools:
            func_tool.add_tool(t)

        self._debug(
            f"[ToolGate] 隐藏 {len(hidden_tools)} 个 | "
            f"保留 {len(kept_tools)} 个白名单 + activate_tools | "
            f"目录 {len(catalog_text)} 字符"
        )

    # ──────────────────────────────────────────────
    #  调试日志 — 同时发到 QQ 和控制台
    # ──────────────────────────────────────────────
    def _debug(self, msg: str):
        if not self.config.get("debug", True):
            return
        logger.info(msg)
        event = self._current_event
        if event is not None:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._send_debug(event, msg))
            except RuntimeError:
                pass

    async def _send_debug(self, event: AstrMessageEvent, msg: str):
        try:
            await event.send(event.plain_result(f"🔧 {msg}"))
        except Exception:
            pass

    async def terminate(self):
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  activate_tools 元工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_activate_tool(
    hidden_tools: list[FunctionTool],
    target_tool_set: ToolSet,
    debug_fn,
) -> FunctionTool:
    """工厂函数：创建 activate_tools 元工具。"""

    async def _activate_handler(event, **kwargs):
        restored = []
        for tool in hidden_tools:
            target_tool_set.add_tool(tool)
            restored.append(tool.name)

        debug_fn(f"[ToolGate] ✅ 解锁 {len(restored)} 个工具: {restored}")

        return (
            f"已成功解锁 {len(restored)} 个工具: {', '.join(restored)}。"
            f"你现在可以直接调用这些工具了。"
        )

    tool = FunctionTool(
        name="activate_tools",
        description=(
            "解锁工具箱。你的工具当前处于待命状态，"
            "当你需要调用任何工具时，必须先调用此工具来解锁。"
            "无需参数，调用即解锁全部工具。"
            "这是一个静默的系统操作，无需向用户提及。"
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=_activate_handler,
    )

    return tool
