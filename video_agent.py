"""
VideoAgent 核心编排器 — 纯 MCP 架构，所有操作通过 MCP Server 完成。

架构:
  GUI (主线程 tkinter)
  │
  ├─→ VideoAgent (编排线程)
  │     ├─→ MCPClient          → MCP Server (stdio 子进程)
  │     │     ├─→ BrowserController (asyncio)
  │     │     ├─→ TranscriberCore
  │     │     ├─→ DeepSeekClient
  │     │     └─→ SessionManager (JSON: sessions/sessions.json)
  │     └─→ DeepSeekClient     (聊天对话，非分析)
  │
  └─→ AgentGUI                 (通过 msg_queue 跨线程通信)

核心原则:
  - 所有浏览器操作 → MCP 工具
  - 所有会话管理   → MCP 工具（JSON 驱动）
  - 所有转录操作   → MCP 工具
  - video_agent.py 是薄编排层，不含任何硬件/浏览器/转录的硬编码逻辑
"""

from __future__ import annotations

import os
import re
import threading
import time

from deepseek_client import DeepSeekClient
from mcp_client import MCPClient, format_tools_for_ds


# ═══════════════════════════════════════════════════════════
#  VideoAgent
# ═══════════════════════════════════════════════════════════

class VideoAgent:
    """
    视频分析 Agent 核心 — 纯 MCP 架构。

    用法:
        agent = VideoAgent(gui, deepseek_client)
        agent.handle_user_message("启动 Agent")   # 启动 MCP Server
        agent.handle_user_message("帮我分析")     # 自然语言指令 → DS 决策 → MCP 执行
        agent.shutdown()
    """

    def __init__(self, gui: "AgentGUI", deepseek: DeepSeekClient):
        self._gui = gui
        self._deepseek = deepseek

        # MCP 客户端（所有操作都通过它）
        self._mcp = MCPClient(log_callback=self._gui.log)

        # 对话历史（DS 多轮）
        self._chat_history: list[dict] = []

        # 防 get_page 死循环计数器（每个用户消息重置）
        self._get_page_depth = 0

    # ═══════════════════════════════════════════════════════
    #  密钥管理
    # ═══════════════════════════════════════════════════════

    def set_api_key(self, key: str) -> bool:
        """设置 API Key 并持久化到 .env 文件。"""
        key = key.strip()
        if not key:
            return False

        self._deepseek = DeepSeekClient(api_key=key)

        try:
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            lines = []
            found = False
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("DEEPSEEK_API_KEY="):
                            lines.append(f"DEEPSEEK_API_KEY={key}\n")
                            found = True
                        else:
                            lines.append(line)
            if not found:
                lines.append(f"DEEPSEEK_API_KEY={key}\n")

            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(lines)

            self._gui.log(f"API Key 已保存到 {env_path}", "success")
            return True
        except Exception as e:
            self._gui.log(f"保存 API Key 失败: {e}", "error")
            return False

    # ═══════════════════════════════════════════════════════
    #  启动 / 关闭
    # ═══════════════════════════════════════════════════════

    def _start_agent(self):
        """启动 Agent：启动 MCP Server 子进程（内部自动连接浏览器）。"""
        self._gui.log("启动 Agent (MCP)...", "accent")
        self._gui.assistant_say("正在启动 MCP Server 并连接浏览器...")

        if not self._mcp.running:
            ok = self._mcp.start()
            if not ok:
                self._gui.assistant_say(
                    "MCP Server 启动失败。\n"
                    "请确认依赖已安装:\n"
                    "  pip install playwright requests soundcard faster-whisper numpy"
                )
                return

        # ── 先尝试被动连接（如果用户已手动启动 Edge 调试模式）──
        time.sleep(1.5)
        state = self._mcp.call("browser_status")
        if state and not state.get("error"):
            page_title = state.get("page_title", "")
            self._gui.log(
                f"已锁定标签页: {page_title[:40] if page_title else '(未知)'}", "dim"
            )
            self._gui.set_status({"connected": True})
            self._gui.assistant_say(
                "Agent 已就绪！可以跟我说「帮我分析」「帮我找视频」或任何你想做的事。"
            )
            return

        # ── 被动连接失败 → 尝试自动启动 Edge（launch_persistent_context）──
        self._gui.log("未检测到运行中的 Edge，自动启动独立 Edge...", "dim")
        self._gui.assistant_say("正在启动独立 Edge（5-15秒）...")
        t0 = time.time()
        result = self._mcp.call("browser_connect", {"auto_start": 1}, timeout=70)
        elapsed = time.time() - t0
        self._gui.log(f"browser_connect 耗时 {elapsed:.1f}s, result keys={list(result.keys()) if isinstance(result, dict) else type(result).__name__}", "dim")

        if result.get("connected"):
            page_title = result.get("page_title", "")
            self._gui.log(f"已锁定标签页: {page_title[:40] if page_title else '(未知)'}", "dim")
            self._gui.set_status({"connected": True})
            self._gui.assistant_say(
                "Edge 已启动！请在 Edge 中打开你要看的页面，\n"
                "然后输入「帮我分析」或你的指令。"
            )
        else:
            msg = result.get("message", "未知错误")
            self._gui.log(f"Edge 启动失败: {msg}", "warn")
            self._gui.assistant_say(
                f"Edge 启动失败。\n"
                f"请确保 Edge 已安装，然后重试。\n"
                f"如果问题持续，请检查 Playwright: pip install playwright chromium"
            )

    def shutdown(self):
        """安全关闭所有组件。"""
        self._gui.log("正在关闭...", "accent")
        try:
            self._mcp.call("analysis_stop")
        except Exception:
            pass
        try:
            self._mcp.call("transcription_stop")
        except Exception:
            pass
        self._mcp.stop()
        self._gui.log("VideoAgent 已关闭", "dim")

    # ═══════════════════════════════════════════════════════
    #  用户指令处理（来自 GUI 聊天框）
    # ═══════════════════════════════════════════════════════

    def handle_user_message(self, text: str):
        """处理用户在聊天框输入的指令。

        架构:
          - 基础设施指令（启动/停止/状态/密钥/帮助）→ 本地直接处理
          - 其他所有指令 → 发给 DeepSeek 聊天 → DS 决策 → MCP 工具执行
        """
        text_lower = text.strip().lower()
        text_raw = text.strip()

        # ── 启动 Agent ──
        if any(kw in text_raw for kw in ("启动agent", "启动Agent", "启动 agent",
                                          "启动 Agent", "连接浏览器")):
            threading.Thread(target=self._start_agent, daemon=True).start()
            return

        # ── 设置密钥 ──
        if text_raw.startswith("设置密钥") or text_raw.startswith("设置 key"):
            parts = text_raw.split(None, 1)
            if len(parts) >= 2:
                key = parts[1].strip()
                if key:
                    ok = self.set_api_key(key)
                    if ok:
                        self._gui.assistant_say("API Key 已保存，重启也有效！现在可以开始分析了。")
                    else:
                        self._gui.assistant_say("密钥保存失败，请检查文件权限。")
                    return
            self._gui.assistant_say("格式: 设置密钥 sk-xxxxxxxx")
            return

        # ── 停止（本地快捷指令）──
        if text_lower in ("停止", "stop", "停止分析"):
            try:
                self._mcp.call("analysis_stop")
                self._mcp.call("transcription_stop")
            except Exception:
                pass
            self._gui.log("分析已停止", "accent")
            self._gui.assistant_say("分析已停止。")
            return

        # ── 状态（本地快捷指令）──
        if text_lower in ("状态", "status"):
            lines = []
            try:
                bs = self._mcp.call("browser_status")
                if bs and not bs.get("error"):
                    lines.append(f"浏览器: 已连接")
                    lines.append(f"页面: {bs.get('page_title', '?')}")
                    lines.append(f"URL: {bs.get('page_url', '?')[:60]}")
                    if bs.get("has_video"):
                        lines.append(
                            f"视频: {bs.get('current_time', 0):.0f}s / "
                            f"{bs.get('duration', 0):.0f}s  "
                            f"{'暂停' if bs.get('paused') else '播放中'}"
                        )
                else:
                    lines.append("浏览器: 未连接")
            except Exception:
                lines.append("浏览器: 查询失败")

            try:
                an = self._mcp.call("analysis_status")
                if an and not an.get("error"):
                    lines.append(f"字幕: {'运行中' if an.get('caption_running') else '未启动'}")
                    lines.append(f"分析: {'运行中' if an.get('running') else '空闲'}")
                    if an.get("running"):
                        lines.append(f"  - 轮次: {an.get('round', 0)}")
                        lines.append(f"  - 截图: {an.get('screenshots_taken', 0)} 张")
            except Exception:
                pass

            lines.append(f"DeepSeek: {'已配置' if self._deepseek.configured else '未配置'}")
            lines.append(f"MCP Server: {'运行中' if self._mcp.running else '未启动'}")

            self._gui.assistant_say("\n".join(lines))
            return

        # ── 帮助 ──
        if text_lower in ("帮助", "help", "?"):
            self._gui.assistant_say(
                "可用指令：\n"
                "  • 启动 Agent / 帮我分析 — 启动 AI 助手\n"
                "  • 帮我找 xxx — 搜索视频并导航\n"
                "  • 停止 — 停止分析\n"
                "  • 状态 — 查看当前状态\n"
                "  • 设置密钥 sk-xxx — 配置 API Key\n"
                "\n"
                "自然语言示例（DS 会自动识别意图并调用 MCP 工具）：\n"
                "  • 帮我分析这个视频\n"
                "  • 继续上次的分析\n"
                "  • 继承上一讲的截图，开新会话看第11讲\n"
                "  • 打开第10讲\n"
                "  • 列出所有历史会话\n"
                "  • 截一张架构图\n"
                "\n"
                "也可以直接输入自然语言指令，Agent 会智能理解你的意图。"
            )
            return

        # ── 通用 DS 聊天（所有未匹配的指令都发给 DeepSeek）──
        threading.Thread(target=self._handle_ds_chat, args=(text_raw,), daemon=True).start()

    # ═══════════════════════════════════════════════════════
    #  DS 聊天（核心编排）
    # ═══════════════════════════════════════════════════════

    def _handle_ds_chat(self, text: str):
        """将用户消息发给 DeepSeek 聊天，执行返回的动作。

        这是唯一的操作入口 — 所有浏览器操作、会话管理、转录、分析
        都通过 DS 决策 → MCP 工具这条路径完成，不存在硬编码捷径。
        """
        if not self._deepseek.configured:
            self._gui.assistant_say("请先配置 DeepSeek API Key。输入「设置密钥 sk-xxx」")
            return

        self._gui.log(f"DS 聊天: {text[:50]}", "accent")

        # 重置 get_page 死循环计数器
        self._get_page_depth = 0

        # ── 收集页面上下文（通过 MCP）──
        page_context = {
            "page_title": "",
            "page_url": "",
            "has_video": False,
            "video_time": 0.0,
            "duration": 0.0,
            "paused": False,
        }
        browser_ok = False
        connected = self._mcp.running
        if connected:
            try:
                state = self._mcp.call("browser_status")
                if state and not state.get("error"):
                    browser_ok = True
                    page_context["page_title"] = state.get("page_title", "")
                    page_context["page_url"] = state.get("page_url", "")
                    page_context["has_video"] = state.get("has_video", False)
                    page_context["video_time"] = state.get("current_time", 0)
                    page_context["duration"] = state.get("duration", 0)
                    page_context["paused"] = state.get("paused", False)
                    self._gui.log(
                        f"browser_status → has_video={page_context['has_video']}, "
                        f"url={page_context['page_url'][:60] or '(空)'}, "
                        f"title={page_context['page_title'][:30]}", "dim"
                    )
            except Exception:
                pass

        video_state = {
            "video_time": page_context["video_time"],
            "duration": page_context["duration"],
            "paused": page_context["paused"],
            "page_title": page_context["page_title"],
            "page_url": page_context["page_url"],
            "has_video": page_context["has_video"],
        } if browser_ok else None

        # ── 附上页面结构 ──
        user_text = text
        if browser_ok:
            structures = []

            if page_context.get("page_title"):
                structures.append(f"[页面] {page_context['page_title'][:60]}")
            if page_context.get("page_url"):
                structures.append(f"[URL] {page_context['page_url'][:80]}")
            if page_context.get("has_video"):
                t = page_context.get("video_time", 0)
                d = page_context.get("duration", 0)
                p = "暂停" if page_context.get("paused") else "播放中"
                structures.append(f"[视频] {t:.0f}s/{d:.0f}s {p}")

            # 非视频页 → 附页面元素
            if not page_context.get("has_video"):
                try:
                    page_data = self._mcp.call("browser_get_page")
                    if page_data and not page_data.get("error"):
                        elements = page_data.get("elements", [])
                        stats = page_data.get("stats", {})
                        if elements:
                            # 兼容新旧格式：旧格式用 "type"，新格式用 "tag"
                            _et = lambda e: e.get("tag") or e.get("type", "")
                            _eh = lambda e: e.get("href") or ""

                            links = [e for e in elements
                                     if _et(e) in ("a", "link", "nav_link", "video_related",
                                                    "content_link", "menuitem", "tab",
                                                    "listitem", "option")]
                            buttons = [e for e in elements
                                       if _et(e) in ("button", "clickable")]
                            headings = [e for e in elements
                                        if _et(e) in ("heading", "h1", "h2", "h3", "h4", "h5", "h6")]
                            iframes = [e for e in elements if _et(e) == "iframe"]
                            videos = [e for e in elements if _et(e) in ("video", "audio")]

                            # 统计摘要 → 让 DS 一眼看懂页面类型
                            if stats:
                                parts = []
                                if stats.get("total_iframes", 0):
                                    parts.append(f"{stats['total_iframes']}个iframe")
                                if stats.get("total_videos", 0):
                                    parts.append(f"{stats['total_videos']}个video")
                                if stats.get("total_links", 0):
                                    parts.append(f"{stats['total_links']}个链接")
                                if stats.get("total_buttons", 0):
                                    parts.append(f"{stats['total_buttons']}个按钮")
                                if stats.get("total_headings", 0):
                                    parts.append(f"{stats['total_headings']}个标题")
                                if parts:
                                    structures.append(f"[页面统计] {' | '.join(parts)}")

                            structures.append(f"[交互元素 {len(elements)}个]")
                            if iframes:
                                structures.append(
                                    "▶ iframe: " + " | ".join(
                                        f"{f['text'][:30] or '(无标题)'}"
                                        + (f"→{f.get('src','')[:50]}" if f.get('src') else "")
                                        for f in iframes[:5]
                                    ))
                            if videos:
                                structures.append(
                                    "▶ video: " + ", ".join(
                                        f"{v['text'][:30] or '(无标题)'}" for v in videos[:3]
                                    ))
                            if links:
                                structures.append(
                                    "链接: " + " | ".join(
                                        f"{l['text'][:25]}"
                                        + (f"→{_eh(l)[:50]}" if _eh(l) else "")
                                        for l in links[:12]
                                    ))
                            if buttons:
                                structures.append("按钮: " + ", ".join(b["text"][:20] for b in buttons[:6]))
                            if headings:
                                structures.append("章节: " + ", ".join(h["text"][:30] for h in headings[:6]))
                        else:
                            structures.append("[页面元素为空，可能还在加载中，请等待页面渲染或用 click/navigate 操作]")
                    else:
                        structures.append("[无法获取页面元素，页面可能未加载完成]")
                except Exception:
                    structures.append("[获取页面元素异常]")

            if structures:
                user_text = text + "\n---\n当前页面：" + "; ".join(structures)
        else:
            # 浏览器未连接 → 明确告诉 DS
            user_text = (
                text +
                "\n---\n[系统] 浏览器尚未连接。你需要先 connect_browser（如果 MCP 支持）或提示用户以调试模式启动 Edge。"
                "\n不要执行 navigate/search/get_page 等浏览器操作，回复用户提示即可。"
            )

        # ── 调用 DS ──
        try:
            result = self._deepseek.chat(
                user_message=user_text,
                video_state=video_state,
                conversation_history=self._chat_history,
            )
        except Exception as e:
            self._gui.assistant_say(f"DeepSeek 调用失败: {e}")
            return

        reply = result.get("reply", "")
        actions = result.get("actions", [])

        # 显示 DS 回复
        if reply:
            self._gui.assistant_say(reply)
        else:
            self._gui.assistant_say("收到！")

        # 记录对话历史 — 附带页面上下文防止 DS 失忆
        # 视频页只记原始消息（有 video_state 通过 system prompt 补全）
        # 非视频页记完整的 user_text（含页面元素），让 DS 记住页面结构
        if page_context.get("has_video"):
            self._chat_history.append({"role": "user", "content": text})
        else:
            self._chat_history.append({"role": "user", "content": user_text[:2000]})
        self._chat_history.append({"role": "assistant", "content": reply})
        if len(self._chat_history) > 40:
            self._chat_history = self._chat_history[-40:]

        # ── 执行 DS 返回的动作（全部走 MCP）──
        if actions:
            self._gui.log(
                f"DS 返回 {len(actions)} 个动作: {[a.get('type') for a in actions]}", "dim"
            )
            state_changed = self._execute_ds_actions(actions, text, page_context)

            if state_changed:
                self._gui.log("页面已变化，自动续问 DS...", "dim")
                self._auto_continue_after_page_change()

    # ═══════════════════════════════════════════════════════
    #  动作执行（纯 MCP 路由）
    # ═══════════════════════════════════════════════════════

    def _execute_ds_actions(self, actions: list[dict], user_text: str = "", page_context: dict | None = None) -> bool:
        """执行 DS 返回的动作指令 — 全部通过 MCP 工具。

        每个动作类型 → 对应的 MCP 工具调用。
        不存在任何绕过 MCP 的硬编码路径。

        Returns:
            True:  页面状态已改变（navigate/search/click）
            False: 页面未变或已内部处理完毕
        """
        needs_page_roundtrip = False
        state_changed = False

        for act in actions:
            t = act.get("type", "")
            try:
                # ── get_page：需要二次 DS 决策 ──
                # 但如果当前页面是视频页，get_page 无意义（已有视频状态，不需找链接）
                # 直接忽略 get_page 并告知 DS 当前在视频页
                if t == "get_page":
                    if page_context and page_context.get("has_video"):
                        self._gui.log("跳过 get_page（当前在视频页，无需探索页面元素）", "dim")
                        self._chat_history.append({
                            "role": "system",
                            "content": (
                                f"（当前在视频页：{page_context.get('page_title', '')}，"
                                f"进度 {page_context.get('video_time', 0):.0f}s/"
                                f"{page_context.get('duration', 0):.0f}s。"
                                f"无需探索页面元素，直接告诉用户当前在哪个视频、建议 analyze 即可。）"
                            )
                        })
                        continue
                    needs_page_roundtrip = True
                    break

                # ── analyze：创建会话 → 转录 → 分析（完整流水线）──
                elif t == "analyze":
                    self._gui.log("DS 请求启动分析", "accent")
                    session_name = user_text[:40] or "分析"
                    sess = self._mcp.call("session_create", {"name": session_name})
                    self._gui.log(f"会话已创建: {sess.get('name', '?')}", "success")
                    trans = self._mcp.call("transcription_start", {"show_gui": 0})
                    self._gui.log(f"转录: {trans.get('status', '?')} → {trans.get('log_path', '?')}", "dim")
                    anal = self._mcp.call("analysis_start")
                    self._gui.log(f"分析: {anal.get('status', '?')}", "dim")
                    self._gui.system_say("分析已启动！AI 将持续监听字幕并自动截图。")
                    return False

                # ── new_session：仅创建会话（继承可选），不启动分析 ──
                elif t == "new_session":
                    name = act.get("name", user_text[:40])
                    title = act.get("title", "")
                    inherit = act.get("inherit_from", "")
                    args = {"name": name}
                    if title:
                        args["title"] = title
                    if inherit:
                        args["inherit_from"] = inherit
                    result = self._mcp.call("session_create", args)
                    self._gui.log(
                        f"DS 创建新会话: {result.get('name', '?')}"
                        + (f" (继承 {inherit})" if inherit else ""),
                        "accent",
                    )
                    self._gui.assistant_say(
                        f"已创建会话: {result.get('name', '?')}"
                        + (f"（继承自 {inherit}，含 {result.get('screenshot_count', 0)} 张截图）" if inherit else "")
                    )

                # ── mcp：DS 直接调用任意 MCP 工具 ──
                elif t == "mcp":
                    tool = act.get("tool", "")
                    args = act.get("args", {})
                    if tool:
                        result = self._mcp.call(tool, args)
                        self._gui.log(f"  MCP {tool}: {str(result)[:80]}", "dim")

                # ── stop ──
                elif t == "stop":
                    self._mcp.call("analysis_stop")
                    self._mcp.call("transcription_stop")
                    self._gui.log("DS 请求停止", "accent")
                    self._gui.assistant_say("已停止。")
                    self._chat_history.append({
                        "role": "system",
                        "content": "（分析/操作已停止。）"
                    })
                    return False

                # ── seek：视频跳转 ──
                elif t == "seek":
                    target = float(act.get("time", 0))
                    self._mcp.call("browser_seek", {"time": target})
                    self._gui.log(f"  seek {target:.0f}s", "dim")

                # ── pause / play ──
                elif t == "pause":
                    self._mcp.call("browser_pause")
                    self._gui.log("  pause", "dim")

                elif t == "play":
                    self._mcp.call("browser_play")
                    self._gui.log("  play", "dim")

                # ── screenshot ──
                elif t == "screenshot":
                    name = act.get("name", "截图")
                    result = self._mcp.call("browser_screenshot", {"name": name})
                    if result.get("path"):
                        fname = os.path.basename(result["path"])
                        self._gui.log(f"  {fname}", "success")
                        self._gui.assistant_say(f"已截图: {name}")

                # ── click：页面交互 ──
                elif t == "click":
                    click_text = act.get("text", "").strip()
                    click_index = int(act.get("index", 0))
                    if click_text:
                        self._gui.log(f"  click '{click_text[:30]}'", "dim")
                        result = self._mcp.call("browser_click", {
                            "text": click_text, "index": click_index
                        })
                        if result.get("clicked"):
                            state_changed = True
                            self._gui.assistant_say(f"已点击「{click_text[:30]}」")
                        else:
                            self._gui.log(f"点击失败: {result.get('error', '未找到元素')}", "warn")

                # ── scroll：页面滚动（触发懒加载查看更多内容）──
                elif t == "scroll":
                    direction = act.get("direction", "down")
                    amount = act.get("amount", 0)
                    self._gui.log(f"  滚动: {direction}", "dim")
                    result = self._mcp.call("browser_scroll", {
                        "direction": direction, "amount": amount
                    })
                    if result.get("ok"):
                        state_changed = True
                        self._gui.assistant_say(f"已滚动页面（{direction}）")
                    else:
                        self._gui.log(f"滚动失败: {result.get('error', '未知')}", "warn")

                # ── navigate：页面导航 ──
                elif t == "navigate":
                    url = act.get("url", "")
                    if url:
                        self._gui.log(f"  导航: {url}", "dim")
                        state_changed = True
                        self._mcp.call("browser_navigate", {"url": url})
                        self._gui.assistant_say(f"已打开 {url}")
                        self._chat_history.append({
                            "role": "system",
                            "content": f"（已导航到 {url}，当前页面即此地址。）"
                        })

                # ── search：B站搜索 ──
                elif t == "search":
                    query = act.get("query", "")
                    if query:
                        search_url = f"https://www.bilibili.com/search?keyword={query}"
                        self._gui.log(f"  搜索: {query}", "dim")
                        state_changed = True
                        self._mcp.call("browser_navigate", {"url": search_url})
                        self._gui.assistant_say(f"已搜索: {query}")
                        self._chat_history.append({
                            "role": "system",
                            "content": f"（已搜索 {query}，当前在搜索结果页。）"
                        })

                # ── status（本地快捷指令的回退）──
                elif t == "status":
                    bs = self._mcp.call("browser_status")
                    an = self._mcp.call("analysis_status")
                    self._gui.assistant_say(
                        f"浏览器: {'已连接' if bs else '未连接'}\n"
                        f"字幕: {'运行中' if an.get('caption_running') else '未启动'}\n"
                        f"分析: {'运行中' if an.get('running') else '空闲'}"
                    )

            except Exception as e:
                self._gui.log(f"  动作 {t} 失败: {e}", "warn")

        # ── get_page 二次决策 ──
        if needs_page_roundtrip:
            self._handle_get_page_roundtrip(user_text)
            return False

        return state_changed

    # ═══════════════════════════════════════════════════════
    #  get_page 二次决策
    # ═══════════════════════════════════════════════════════

    def _handle_get_page_roundtrip(self, user_text: str):
        """获取页面结构 → 回传 DS 做二次决策（找链接/按钮）。

        内置死循环防护：连续 get_page 超过 3 次则强制中断。
        """
        self._get_page_depth += 1
        if self._get_page_depth > 3:
            self._gui.log(f"get_page 已达 {self._get_page_depth} 次，强制中断防止死循环", "warn")
            self._gui.assistant_say("页面元素获取异常，请手动操作浏览器。")
            self._chat_history.append({
                "role": "system",
                "content": "（get_page 已调用多次且未找到目标。不要再输出 get_page 动作，直接告诉用户当前页面状态。）"
            })
            return

        self._gui.log(f"DS 请求页面结构 (第{self._get_page_depth}次)，正在提取（MCP）...", "accent")
        page_data = self._mcp.call("browser_get_page")

        elements = page_data.get("elements", [])
        page_title = page_data.get("title", "")
        page_url = page_data.get("url", "")
        page_source = page_data.get("source", "mcp")
        stats = page_data.get("stats", {})

        # 兼容新旧格式
        _et = lambda e: e.get("tag") or e.get("type", "")
        _eh = lambda e: e.get("href") or ""

        links = [e for e in elements
                 if _et(e) in ("a", "link", "nav_link", "video_related",
                                "content_link", "menuitem", "tab",
                                "listitem", "option", "clickable")]
        buttons = [e for e in elements
                   if _et(e) in ("button", "clickable")]
        headings = [e for e in elements
                    if _et(e) in ("heading", "h1", "h2", "h3", "h4", "h5", "h6")]
        search_boxes = [e for e in elements
                        if _et(e) == "search_box"
                        or (_et(e) in ("input", "select", "textarea")
                            and e.get("input_type") in ("search", "text"))]
        iframes = [e for e in elements if _et(e) == "iframe"]
        videos = [e for e in elements if _et(e) in ("video", "audio")]

        page_summary_parts = [f"当前页面: {page_title}", f"URL: {page_url}"]
        # 统计摘要
        if stats:
            stat_parts = [f"{k}:{v}" for k, v in stats.items() if v]
            if stat_parts:
                page_summary_parts.append(f"页面统计: {', '.join(stat_parts)}")
        page_summary_parts.append(f"\n可见交互元素共 {len(elements)} 个 (来源: {page_source}):\n")
        if iframes:
            page_summary_parts.append(f"【iframe】({len(iframes)}个):")
            page_summary_parts.append("\n".join(
                f"  - {f['text'][:60] or '(无标题)'}"
                + (f"  src={f.get('src','')[:80]}" if f.get('src') else "")
                for f in iframes[:8]))
        if videos:
            page_summary_parts.append(f"\n【video/audio】({len(videos)}个):")
            page_summary_parts.append(self._fmt_page_items(videos[:5]))
        if links:
            page_summary_parts.append(f"\n【链接】({len(links)}个):")
            page_summary_parts.append(self._fmt_page_items(links[:20]))
        if buttons:
            page_summary_parts.append(f"\n【按钮】({len(buttons)}个):")
            page_summary_parts.append(self._fmt_page_items(buttons[:15]))
        if headings:
            page_summary_parts.append(f"\n【标题/章节】({len(headings)}个):")
            page_summary_parts.append(self._fmt_page_items(headings[:10]))
        if search_boxes:
            page_summary_parts.append(f"\n【搜索框】({len(search_boxes)}个):")
            page_summary_parts.append(self._fmt_page_items(search_boxes[:5]))
        page_summary = "\n".join(page_summary_parts)

        self._gui.log(f"页面结构: {len(elements)} 个元素 -> 回传 DS", "dim")

        # 如果没有可交互元素，提示 DS 这可能是内容页面，不要再 get_page
        if len(elements) == 0:
            hint = (
                "【注意】该页面没有提取到任何可交互元素（无链接、无按钮、无标题）。"
                "这通常意味着页面是纯视频/纯内容页（iframe 播放器、canvas 渲染等），"
                "不是可导航的列表页。请不要再输出 get_page，"
                f"直接根据当前页面信息（标题={page_title or '(未知)'}, URL={page_url or '(未知)'}）回复用户。"
            )
            follow_msg = (
                f"用户原话「{user_text}」。\n\n{hint}\n\n{page_summary}"
            )
        else:
            follow_msg = (
                f"请分析以下页面结构，用户原话「{user_text}」。"
                f"找到最匹配的链接并导航：\n\n{page_summary}"
            )
        self._chat_history.append({"role": "user", "content": follow_msg})

        try:
            result = self._deepseek.chat(
                user_message=follow_msg,
                conversation_history=self._chat_history,
            )
        except Exception as e:
            self._gui.assistant_say(f"DS 二次调用失败: {e}")
            return

        reply = result.get("reply", "")
        follow_actions = result.get("actions", [])
        if reply:
            self._gui.assistant_say(reply)
        self._chat_history.append({"role": "assistant", "content": reply})

        if follow_actions:
            self._gui.log(
                f"DS 二次返回 {len(follow_actions)} 动作: "
                f"{[a.get('type') for a in follow_actions]}",
                "dim",
            )
            changed = self._execute_ds_actions(follow_actions, user_text)
            if changed:
                self._gui.log("二次决策后页面变化，自动续问 DS...", "dim")
                self._auto_continue_after_page_change()

    # ═══════════════════════════════════════════════════════
    #  自动续问（navigate/search/click 后）
    # ═══════════════════════════════════════════════════════

    def _auto_continue_after_page_change(self):
        """页面变化后自动获取新页面上下文，让 DS 看新页面并回复用户。"""
        structures = []
        has_video = False
        try:
            state = self._mcp.call("browser_status")
            if state and not state.get("error"):
                has_video = state.get("has_video", False)
                if state.get("page_title"):
                    structures.append(f"[页面] {state['page_title'][:60]}")
                if state.get("page_url"):
                    structures.append(f"[URL] {state['page_url'][:80]}")
                if has_video:
                    t = state.get("current_time", 0)
                    d = state.get("duration", 0)
                    p = "暂停" if state.get("paused") else "播放中"
                    structures.append(f"[视频] {t:.0f}s/{d:.0f}s {p}")
            else:
                structures.append("[浏览器状态获取失败，可能页面还在加载]")
        except Exception:
            structures.append("[浏览器状态异常]")

        # 非视频页 → 附页面元素
        if not has_video:
            try:
                page_data = self._mcp.call("browser_get_page")
                if page_data and not page_data.get("error"):
                    elements = page_data.get("elements", [])
                    if elements:
                        links = [e for e in elements
                                 if e["type"] in ("link", "nav_link", "video_related", "content_link",
                                                  "menuitem", "tab", "listitem", "option")]
                        buttons = [e for e in elements if e["type"] in ("button", "clickable")]
                        structures.append(f"[交互元素 {len(elements)}个]")
                        if links:
                            structures.append("链接: " + " | ".join(
                                f"{l['text'][:25]}" +
                                (f"→{l.get('href','')[:50]}" if l.get('href') else "")
                                for l in links[:8]
                            ))
                        if buttons:
                            structures.append("按钮: " + ", ".join(b["text"][:15] for b in buttons[:5]))
                    else:
                        structures.append("[页面元素为空，可能还在加载]")
            except Exception:
                pass

        page_desc = "; ".join(structures) if structures else "（无法获取页面信息）"

        follow_msg = (
            f"上一轮操作已完成。当前页面内容:\n{page_desc}\n\n"
            "请基于这些信息给用户一个自然的回复"
            "（描述页面内容/问用户想看什么/如果需要进一步操作则附加 JSON 动作）。"
            + ("\n⚠ 如果当前页面是视频页，直接输出 analyze！" if has_video else "")
        )
        self._chat_history.append({"role": "user", "content": follow_msg})

        try:
            result = self._deepseek.chat(
                user_message=follow_msg,
                conversation_history=self._chat_history,
            )
        except Exception as e:
            self._gui.log(f"自动续问 DS 失败: {e}", "warn")
            return

        reply = result.get("reply", "")
        follow_actions = result.get("actions", [])
        if reply:
            self._gui.assistant_say(reply)
        self._chat_history.append({"role": "assistant", "content": reply})

        if follow_actions:
            self._gui.log(f"续问返回 {len(follow_actions)} 动作", "dim")
            self._execute_ds_actions(follow_actions, "")

    # ═══════════════════════════════════════════════════════
    #  工具方法
    # ═══════════════════════════════════════════════════════

    def _fmt_page_items(self, items: list[dict], max_items: int = 20) -> str:
        """格式化页面元素为 DS 易读的列表。"""
        lines = []
        for i, item in enumerate(items[:max_items]):
            t = item["text"][:60]
            href = item.get("href", "")[:80]
            lines.append(f"  [{i}] {t}")
            if href:
                lines.append(f"      → {href}")
        return "\n".join(lines) if lines else "  (无)"


# 类型引用（避免循环导入）
from agent_gui import AgentGUI
