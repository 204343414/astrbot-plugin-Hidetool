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
        # 记录当前正在处理的 event，供 debug 消息发送用
        self._current_event: AstrMessageEvent | None = None

    # ──────────────────────────────────────────────
    #  核心 Hook: 在 LLM 请求前拦截工具列表
    # ──────────────────────────────────────────────
    @filter.on_llm_request(priority=-99)  # 低优先级 = 最后执行，让其他插件先注册完工具
    async def intercept_tools(self, event: AstrMessageEvent, req: ProviderRequest):
        self._current_event = event

        # ── 检查开关 ──
        if not self.config.get("enabled", True):
            return

        # ── 获取当前工具集 ──
        func_tool: ToolSet | None = req.func_tool
        if func_tool is None or func_tool.empty():
            return

        # 拿到所有工具的引用列表
        all_tools: list[FunctionTool] = list(func_tool.tools)  # 浅拷贝一份
        if not all_tools:
            return

        # ── 工具太少，不值得懒加载 ──
        min_count = self.config.get("min_tools_to_activate", 3)
        if len(all_tools) < min_count:
            self._debug(
                f"[ToolGate] 工具数 {len(all_tools)} < 阈值 {min_count}，跳过"
            )
            return

        # ── 防止重复拦截 ──
        tool_names = [t.name for t in all_tools]
        if "activate_tools" in tool_names:
            return

        self._debug(f"[ToolGate] 🔒 拦截 {len(all_tools)} 个工具: {tool_names}")

        # ── 分离: 始终激活的 vs 需要隐藏的 ──
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
            "\n<available_tools_catalog>\n"
            "以下工具当前处于待命状态。如果你需要使用任何工具来完成用户的请求，"
            "请先调用 activate_tools 来激活它们：\n"
            + "\n".join(catalog_lines)
            + "\n</available_tools_catalog>"
        )

        # ── 注入目录到临时上下文（不进历史记录） ──
        req.extra_user_content_parts.append(TextPart(text=catalog_text).mark_as_temp())

        # ── 创建 activate_tools 元工具 ──
        activate_tool = _make_activate_tool(
            hidden_tools=hidden_tools,
            target_tool_set=func_tool,
            debug_fn=self._debug,
        )

        # ── 替换工具集: 清空 → 只放 activate_tools + 白名单 ──
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
        # 控制台始终打印
        logger.info(msg)
        # 同时发到当前会话的 QQ
        event = self._current_event
        if event is not None:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._send_debug(event, msg))
            except RuntimeError:
                pass  # 没有 event loop 就算了

    async def _send_debug(self, event: AstrMessageEvent, msg: str):
        """安全地把 debug 信息发到 QQ。"""
        try:
            await event.send(event.plain_result(f"🔧 {msg}"))
        except Exception:
            pass  # debug 消息发送失败不影响主流程

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
    """
    工厂函数：创建 activate_tools FunctionTool 实例。
    通过 handler 字段注入闭包，框架会走 handler 执行路径。
    """

    async def _activate_handler(event, **kwargs):
        """
        handler 签名: async def handler(event: AstrMessageEvent, **kwargs)
        框架在 _execute_local 中会把 event 作为第一个参数传入。
        """
        restored = []
        for tool in hidden_tools:
            target_tool_set.add_tool(tool)
            restored.append(tool.name)

        debug_fn(f"[ToolGate] ✅ 解锁 {len(restored)} 个工具: {restored}")

        return (
            f"已成功激活 {len(restored)} 个工具: {', '.join(restored)}。"
            f"你现在可以直接调用这些工具了。"
        )

    # 通过 handler 字段注入，这是框架 _execute_local 认可的执行路径
    tool = FunctionTool(
        name="activate_tools",
        description=(
            "激活所有待命工具。在调用其他工具之前，你必须先调用此工具来解锁它们。"
            "此工具无需任何参数。"
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=_activate_handler,
    )

    return tool
