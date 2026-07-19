"""
VideoAgent 核心编排器 — 串联浏览器、转录、DeepSeek、GUI。

架构:
  GUI (主线程 tkinter)
  │
  ├─→ VideoAgent (编排线程)
  │     ├─→ BrowserController (asyncio 线程)
  │     ├─→ TranscriberCore   (后台线程，transcriber_core)
  │     ├─→ DeepSeekClient    (同步 HTTP 调用)
  │     └─→ AgentGUI          (通过 msg_queue 跨线程通信)
"""

from __future__ import annotations

import asyncio
import queue
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from deepseek_client import DeepSeekClient
from browser_controller import BrowserController, CaptureResult


# ── 转录核心（来自 transcriber_core） ──
try:
    from transcriber_core import (
        AudioCapture, Transcriber, check_dependencies as check_transcriber_deps,
    )
    HAS_TRANSCRIBER = True
except ImportError:
    HAS_TRANSCRIBER = False


# ═══════════════════════════════════════════════════════════
#  VideoAgent
# ═══════════════════════════════════════════════════════════

class VideoAgent:
    """
    视频分析 Agent 核心。

    用法:
        agent = VideoAgent(gui, deepseek_client)
        agent.connect_browser()
        agent.start_caption()
        agent.start_analysis()
        # 或通过 GUI 聊天指令控制
        agent.handle_user_message("开始分析")
    """

    def __init__(self, gui: "AgentGUI", deepseek: DeepSeekClient):
        self._gui = gui
        self._deepseek = deepseek

        # 浏览器
        self._browser = BrowserController()
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_thread: threading.Thread | None = None

        # 转录
        self._capture = None    # AudioCapture
        self._transcriber = None  # Transcriber
        self._caption_log_path = ""
        self._caption_start_time = 0.0
        self._caption_running = False

        # 分析状态
        self._analyzing = False
        self._stop_requested = False
        self._analysis_thread: threading.Thread | None = None

        # 截图目录
        self._screenshot_dir = ""
        self._session_dir = ""   # 当前会话文件夹

        # 状态
        self._last_video_time = 0.0
        self._video_duration = 0.0
        self._last_caption_line = 0   # 已读到的字幕行号（防重复）
        self._chat_history: list[dict] = []  # DS 多轮对话记录

    # ═══════════════════════════════════════════════════════
    #  密钥管理（写入 .env，重启持久化）
    # ═══════════════════════════════════════════════════════

    def set_api_key(self, key: str) -> bool:
        """设置 API Key 并持久化到 .env 文件。返回是否成功。"""
        key = key.strip()
        if not key:
            return False

        # 更新内存中的客户端
        self._deepseek = DeepSeekClient(api_key=key)

        # 写入 .env（持久化）
        try:
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            # 读取现有内容
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
    #  asyncio 桥接（浏览器操作在线程中运行）
    # ═══════════════════════════════════════════════════════

    def _start_async_loop(self):
        """启动 asyncio 事件循环线程（供浏览器操作使用）。"""
        if self._async_loop is not None and not self._async_loop.is_closed():
            return  # 已有运行中的事件循环
        def _run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._async_loop = loop
            loop.run_forever()
        self._async_thread = threading.Thread(target=_run_loop, daemon=True)
        self._async_thread.start()
        # 等 loop 就绪
        while self._async_loop is None:
            time.sleep(0.05)

    def _run_async(self, coro, timeout: float = 60):
        """在 asyncio 线程中执行协程，阻塞等待结果。"""
        if self._async_loop is None:
            raise RuntimeError("asyncio 事件循环未启动")
        future = asyncio.run_coroutine_threadsafe(coro, self._async_loop)
        return future.result(timeout=timeout)

    def _stop_async_loop(self):
        """停止 asyncio 事件循环。"""
        if self._async_loop:
            self._async_loop.call_soon_threadsafe(self._async_loop.stop)
        if self._async_thread and self._async_thread.is_alive():
            self._async_thread.join(timeout=3)

    # ═══════════════════════════════════════════════════════
    #  连接
    # ═══════════════════════════════════════════════════════

    def _connect_only(self) -> bool:
        """仅连接浏览器，不查找视频页面。返回是否连上。"""
        self._gui.log("正在连接浏览器（CDP 直连）...", "accent")
        self._start_async_loop()
        ok = self._run_async(self._browser.connect(auto_start=False))
        if not ok:
            self._gui.log("浏览器连接失败", "error")
            return False
        self._gui.log("浏览器已连接 ✓", "success")
        self._gui.set_status({"connected": True})
        return True

    def _find_video_page(self) -> bool:
        """在已连接的浏览器中定位视频页面。返回是否找到。"""
        pages = self._run_async(self._browser.list_pages())
        self._gui.log(f"发现 {len(pages)} 个标签页:")
        for i, p in enumerate(pages):
            icon = "▶" if p.get("has_video") else " "
            vis = "👁" if p.get("visible") else "💤"
            title = p['title'][:40] if p['title'] else "(无标题)"
            self._gui.log(f"  [{i}] {vis} {icon} {title}")
            self._gui.log(f"      {p['url'][:60]}", "dim")

        found = self._run_async(self._browser.select_video_page())
        if not found:
            self._gui.log("未找到含视频的标签页", "warn")
            return False

        state = self._run_async(self._browser.get_state())
        if state:
            self._video_duration = state.get("duration", 0)
            self._last_video_time = state.get("current_time", 0)
            dur_str = f"{state['duration']:.0f}s" if state['duration'] else "未知"
            self._gui.log(
                f"视频: {state['page_title'][:50]}  |  时长: {dur_str}",
                "success",
            )
            self._gui.set_title(state["page_title"][:40])

        self._gui.set_status({
            "connected": True,
            "caption_running": self._caption_running,
            "video_time": self._last_video_time,
            "duration": self._video_duration,
        })
        self._gui.log("视频页已定位 ✓", "success")
        return True

    def connect_browser(self, auto_start: bool = False) -> bool:
        """连接 Edge 并自动定位视频页面。

        auto_start=False: 仅连接已有 CDP
        auto_start=True:  提示用户用桌面「Edge (调试模式)」快捷方式启动
        """
        self._gui.log("正在连接浏览器...", "accent")
        self._start_async_loop()

        # connect 含自动重启 Edge，给 120s 超时
        try:
            ok = self._run_async(self._browser.connect(auto_start=auto_start), timeout=120)
        except Exception as e:
            self._gui.log(f"浏览器连接异常: {e}", "error")
            self._gui.system_say(
                "浏览器连接超时。请关闭所有 Edge 窗口后重试。"
            )
            self._gui.set_status({"connected": False})
            return False
        if not ok:
            self._gui.system_say(
                "未检测到 Edge 调试模式。\n请关闭 Edge，用桌面「Edge (调试模式)」快捷方式重新打开，再输入「帮我分析」。"
            )
            self._gui.set_status({"connected": False})
            return False

        # CDP 连上了 — 尝试定位视频页
        self._find_video_page()
        return True

    # ═══════════════════════════════════════════════════════
    #  转录
    # ═══════════════════════════════════════════════════════

    def start_caption(self, show_gui: bool = False, save_dir: str = "") -> bool:
        """启动 Whisper 转录。

        Args:
            show_gui: 是否显示悬浮字幕窗
            save_dir: 字幕保存目录（默认脚本根目录）
        """
        if not HAS_TRANSCRIBER:
            self._gui.log("转录模块未安装，请检查 transcriber_core.py", "error")
            return False

        if not check_transcriber_deps():
            self._gui.log("转录依赖缺失，请安装 soundcard, faster-whisper", "error")
            return False

        import queue as qmod

        if not save_dir:
            save_dir = os.path.dirname(os.path.abspath(__file__))

        # 字幕文件写入指定目录
        self._caption_log_path = os.path.join(
            save_dir, f"captions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
        self._caption_start_time = time.time()

        audio_q = qmod.Queue()

        # on_text 回调：转发到 GUI 日志
        def on_text(text: str):
            # 不逐条写日志（太吵），只在分析时批量读取
            pass

        self._capture = AudioCapture(audio_q)
        self._transcriber = Transcriber(audio_q, on_text, save_path=self._caption_log_path)

        self._capture.start()
        self._transcriber.start()
        self._caption_running = True

        self._gui.log(f"字幕录制已开始 → {os.path.basename(self._caption_log_path)}", "success")
        if show_gui:
            self._gui.log("（悬浮字幕窗口已打开）", "dim")

        self._gui.set_status({
            "connected": self._browser.connected,
            "caption_running": True,
            "video_time": self._last_video_time,
        })
        return True

    def stop_caption(self):
        """停止转录。"""
        if self._capture:
            self._capture.stop()
            self._capture = None
        if self._transcriber:
            self._transcriber.stop()
            self._transcriber = None
        self._caption_running = False
        self._gui.log("字幕录制已停止", "accent")

    def _read_caption_log(self, tail: int = 100) -> str:
        """读取字幕日志，返回最近 N 行（过滤掉标题和分隔符）。用于一次性查看。"""
        if not self._caption_log_path or not os.path.exists(self._caption_log_path):
            return ""
        with open(self._caption_log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        content = [
            l.rstrip() for l in lines
            if not l.startswith("===") and l.strip() != "---" and l.strip()
        ]
        recent = content[-tail:] if len(content) > tail else content
        return "\n".join(recent)

    def _read_new_captions(self, max_lines: int = 80) -> str:
        """只读上次分析之后新增的字幕行，避免 DS 重复处理。

        用行号追踪：记录已读行数，每次只取新增的。
        首次调用返回最近 max_lines 行。
        """
        if not self._caption_log_path or not os.path.exists(self._caption_log_path):
            return ""

        with open(self._caption_log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        content = [
            l.rstrip() for l in lines
            if not l.startswith("===") and l.strip() != "---" and l.strip()
        ]

        if self._last_caption_line >= len(content):
            return ""  # 没有新行

        # 取新增的行
        new_lines = content[self._last_caption_line:]
        # 更新进度
        self._last_caption_line = len(content)

        # 限制最大行数
        if len(new_lines) > max_lines:
            new_lines = new_lines[-max_lines:]

        return "\n".join(new_lines)

    # ═══════════════════════════════════════════════════════
    #  分析循环
    # ═══════════════════════════════════════════════════════

    def start_analysis(self):
        """启动自动分析循环（在后台线程运行）。"""
        if self._analyzing:
            self._gui.log("分析已在运行中", "warn")
            return

        self._stop_requested = False
        self._analyzing = True
        self._analysis_thread = threading.Thread(target=self._analysis_loop, daemon=True)
        self._analysis_thread.start()
        self._gui.log("自动分析已启动", "accent")
        self._gui.system_say("🚀 自动分析开始！AI 将持续监听字幕并自动截图。")

    def stop_analysis(self):
        """停止分析循环。"""
        self._stop_requested = True
        self._analyzing = False

    def _analysis_loop(self):
        """
        分析主循环（运行在后台线程）。

        每分钟一轮：
          1. 读视频状态（时间 + 暂停/播放）→ 喂给 DS
          2. 读最近 60s 字幕
          3. DS 返回动作列表（seek → pause → screenshot → play）
          4. 按序执行动作
          5. 等 60s，重复
        """
        if not self._browser.has_video_page:
            self._gui.log("未连接视频页，分析中止", "error")
            self._analyzing = False
            return

        # 先等字幕积累 60 秒
        self._gui.assistant_say("⏳ 等待字幕积累 60 秒后开始首轮分析...")
        self._gui.log("等待字幕积累 (60s)...", "dim")
        self._last_caption_line = 0  # 重置已读行号
        for _ in range(60):
            if self._stop_requested:
                self._analyzing = False
                return
            time.sleep(1)

        round_num = 0
        while not self._stop_requested and self._analyzing:
            round_num += 1
            self._gui.log(f"--- 第 {round_num} 轮分析 ---", "accent")

            # 1. 获取视频状态
            video_state = {}
            try:
                state = self._run_async(self._browser.get_state())
                if state:
                    self._last_video_time = state["current_time"]
                    self._video_duration = state.get("duration", 0)
                    video_state = {
                        "video_time": self._last_video_time,
                        "duration": self._video_duration,
                        "paused": state.get("paused", False),
                    }
                    icon = "⏸" if state.get("paused") else "▶"
                    self._gui.log(
                        f"状态: {icon} {self._last_video_time:.0f}s / {self._video_duration:.0f}s | "
                        f"选择器: {self._browser._video_selector}",
                        "dim",
                    )

                    # 检测视频是否播完
                    dur = self._video_duration
                    if dur > 0 and self._last_video_time > 0:
                        remaining = dur - self._last_video_time
                        if remaining <= 3 or (state.get("paused") and remaining <= 10):
                            self._gui.log(f"视频已播完 (剩余 {remaining:.0f}s)，处理最后一轮后停止", "accent")
                            self._gui.assistant_say("视频已接近尾声，最后检查一次...")
                            self._stop_requested = True  # 本轮之后停止
                self._gui.set_status({
                    "connected": True,
                    "caption_running": self._caption_running,
                    "video_time": self._last_video_time,
                    "duration": self._video_duration,
                })
            except Exception as e:
                self._gui.log(f"读取视频状态失败: {e}", "error")

            # 2. 读字幕（只读新增，不重复）
            src_name = os.path.basename(self._caption_log_path) if self._caption_log_path else "?"
            subtitle = self._read_new_captions(max_lines=80)
            line_count = subtitle.count("\n") + 1 if subtitle.strip() else 0
            if line_count == 0:
                self._gui.log("字幕为空，跳过本轮", "dim")
                time.sleep(10)
                continue

            self._gui.log(
                f"DS 读取: {src_name} (最近 {line_count} 行) | 内容预览: {subtitle[:80].strip()}...",
                "dim",
            )

            # 3. DeepSeek 分析（带视频状态）
            try:
                self._gui.log("→ 发送给 DeepSeek...", "accent")
                result = self._deepseek.analyze_screenshots(subtitle, video_state)
                summary = result.get("summary", "")
                actions = result.get("actions", [])
                screenshot_count = sum(1 for a in actions if a["type"] == "screenshot")
                self._gui.log(
                    f"DS 返回: {screenshot_count} 张截图, {len(actions)} 个动作",
                    "success",
                )
                if summary:
                    self._gui.assistant_say(f"[第{round_num}轮] {summary}")
            except Exception as e:
                self._gui.log(f"DeepSeek 异常: {e}", "error")
                time.sleep(10)
                continue

            if not actions:
                self._gui.log("本轮无截图 (DS 跳过)", "dim")
                time.sleep(10)
                continue

            # 4. 按序执行动作
            self._gui.log(f"执行 {len(actions)} 个动作:", "accent")
            try:
                for act in actions:
                    if self._stop_requested:
                        break
                    atype = act["type"]
                    if atype == "seek":
                        t = act.get("time", 0)
                        r = self._run_async(self._browser.seek(t))
                        ok = "" if r.get("ok") else f" 失败: {r.get('error')}"
                        self._gui.log(f"  ⏩ seek {t:.0f}s{ok}", "dim")
                        time.sleep(0.5)
                    elif atype == "pause":
                        self._run_async(self._browser.pause())
                        self._gui.log("  ⏸ pause", "dim")
                        time.sleep(0.3)
                    elif atype == "screenshot":
                        name = act.get("name", "截图")
                        current_time = self._run_async(
                            self._browser.get_current_time()
                        )
                        results = self._run_async(
                            self._browser.capture_batch(
                                [{"time": current_time, "name": name}],
                                save_dir=self._screenshot_dir,
                            )
                        )
                        for r in results:
                            fname = os.path.basename(r["path"])
                            size = os.path.getsize(r["path"])
                            self._gui.log(f"  📸 {fname} ({size//1024}KB)", "success")
                    elif atype == "play":
                        self._run_async(self._browser.play())
                        self._gui.log("  ▶ play", "dim")
                        time.sleep(0.2)
            except Exception as e:
                self._gui.log(f"动作异常: {e}", "error")

            # 5. 等 60 秒再分析下一段
            self._gui.log("等待 60s 后分析下一段...", "dim")
            for _ in range(60):
                if self._stop_requested:
                    break
                time.sleep(1)

        # 分析结束
        self._analyzing = False
        self._finish_session()

    def _finish_session(self):
        """分析结束后：统计截图数量。字幕已在会话文件夹中，无需复制。"""
        if not self._session_dir or not os.path.isdir(self._session_dir):
            self._gui.log("分析循环已结束", "accent")
            self._gui.system_say("✅ 分析完成！")
            return

        # 统计截图
        screenshot_count = 0
        screenshot_dir = os.path.join(self._session_dir, "screenshots")
        if os.path.isdir(screenshot_dir):
            screenshot_count = sum(1 for f in os.listdir(screenshot_dir) if f.endswith(".png"))

        self._gui.log("分析循环已结束", "accent")
        self._gui.system_say(
            f"✅ 分析完成！\n"
            f"共 {screenshot_count} 张截图\n"
            f"保存位置: sessions/{os.path.basename(self._session_dir)}/"
        )

    # ═══════════════════════════════════════════════════════
    #  用户指令处理（来自 GUI 聊天框）
    # ═══════════════════════════════════════════════════════

    def handle_user_message(self, text: str):
        """处理用户在聊天框输入的指令。"""
        text_lower = text.strip().lower()
        text_raw = text.strip()

        # ── 启动 Agent（仅连接浏览器） ──
        if any(kw in text_raw for kw in ("启动agent", "启动Agent", "启动 agent",
                                          "启动 Agent", "连接浏览器")):
            threading.Thread(target=self._start_agent, daemon=True).start()
            return

        # ── 新会话（清空旧文件夹，后续走 DS 决策） ──
        if any(kw in text_raw for kw in ("新会话", "新建会话", "开新的")):
            self._session_dir = ""
            self._gui.assistant_say("已清空会话上下文。")

        # ── 扫描播放器 ──
        if any(kw in text_raw for kw in ("扫描播放器", "扫描视频", "检测播放器", "识别视频")):
            self._scan_player()
            return

        # ── 设置密钥 ──
        if text_raw.startswith("设置密钥") or text_raw.startswith("设置 key"):
            parts = text_raw.split(None, 1)
            if len(parts) >= 2:
                key = parts[1].strip()
                if key:
                    ok = self.set_api_key(key)
                    if ok:
                        self._gui.assistant_say("✅ API Key 已保存，重启也有效！现在可以开始分析了。")
                    else:
                        self._gui.assistant_say("❌ 密钥保存失败，请检查文件权限。")
                    return
            self._gui.assistant_say("格式: 设置密钥 sk-xxxxxxxx")
            return

        # ── 停止 ──
        elif text_lower in ("停止", "stop", "停止分析"):
            self.stop_analysis()
            self._gui.assistant_say("分析已停止。")

        # ── 连接浏览器 ──
        elif text_lower in ("连接浏览器", "connect", "连接"):
            if self._browser.connected:
                self._gui.assistant_say("浏览器已连接。输入「启动 Agent」或任何指令即可。")
            else:
                ok = self.connect_browser(auto_start=False)
                if ok:
                    self._gui.assistant_say("浏览器连接成功！输入「启动 Agent」开始。")

        # ── 启动 Edge 调试模式 ──
        elif text_lower in ("启动 edge", "启动edge", "edge 调试", "打开 edge", "自动启动 edge"):
            self._gui.assistant_say("正在启动 Edge 调试模式...")
            ok = self.connect_browser(auto_start=True)
            if ok:
                self._gui.assistant_say(
                    "Edge 已启动！请在 Edge 中打开你要看的页面，\n"
                    "然后输入「启动 Agent」开始。"
                )
            else:
                self._gui.assistant_say(
                    "Edge 启动失败。请手动启动：\n"
                    "  msedge.exe --remote-debugging-port=9222"
                )

        # ── 状态 ──
        elif text_lower in ("状态", "status"):
            status_lines = [
                f"浏览器: {'已连接' if self._browser.connected else '未连接'}",
                f"视频页: {'已定位' if self._browser.has_video_page else '未定位'}",
                f"字幕: {'录制中' if self._caption_running else '未启动'}",
                f"分析: {'运行中' if self._analyzing else '空闲'}",
                f"视频时间: {self._last_video_time:.0f}s / {self._video_duration:.0f}s",
                f"字幕文件: {os.path.basename(self._caption_log_path) if self._caption_log_path else '无'}",
                f"截图目录: {self._screenshot_dir or '无'}",
                f"DeepSeek: {'已配置' if self._deepseek.configured else '未配置'}",
            ]
            self._gui.assistant_say("\n".join(status_lines))

        # ── 手动截图 ──
        elif text_lower in ("截图", "screenshot", "手动截图"):
            self._manual_screenshot()

        # ── 帮助 ──
        elif text_lower in ("帮助", "help", "?"):
            self._gui.assistant_say(
                "可用指令：\n"
                "  • 启动 Agent / 帮我分析 — 启动 AI 助手（自动判断当前页面做什么）\n"
                "  • 帮我找 xxx — 搜索视频并导航\n"
                "  • 连接浏览器 — 连到已开启 CDP 的 Edge\n"
                "  • 启动 Edge — 自动开启 Edge 调试模式\n"
                "  • 停止 — 停止分析\n"
                "  • 截图 — 手动截图当前帧\n"
                "  • 状态 — 查看当前状态\n"
                "  • 新会话 — 强制新建分析文件夹\n"
                "  • 设置密钥 sk-xxx — 配置 API Key\n"
                "\n"
                "也可以直接输入自然语言指令，Agent 会智能理解你的意图。"
            )

        # ── 通用 DS 聊天（所有未匹配的指令都发给 DeepSeek）──
        else:
            threading.Thread(target=self._handle_ds_chat, args=(text_raw,), daemon=True).start()

    def _handle_ds_chat(self, text: str):
        """将用户消息发给 DeepSeek 聊天，执行返回的动作。"""
        if not self._deepseek.configured:
            self._gui.assistant_say("请先配置 DeepSeek API Key。输入「设置密钥 sk-xxx」或在 ⚙ 面板中设置。")
            return

        self._gui.log(f"DS 聊天: {text[:50]}", "accent")

        # 始终收集页面上下文（标题、URL、是否有视频），不只是 video_state
        page_context = {
            "page_title": "",
            "page_url": "",
            "has_video": False,
            "video_time": 0.0,
            "duration": 0.0,
            "paused": False,
        }
        if self._browser.connected:
            try:
                state = self._run_async(self._browser.get_state(), timeout=5)
                if state:
                    page_context["page_title"] = state.get("page_title", "")
                    page_context["page_url"] = state.get("page_url", "")
                    page_context["has_video"] = state.get("has_video", False)
                    page_context["video_time"] = state.get("current_time", 0)
                    page_context["duration"] = state.get("duration", 0)
                    page_context["paused"] = state.get("paused", False)
            except Exception:
                pass

        # 构建给 DS 的 video_state（兼容现有 chat 接口）
        video_state = {
            "video_time": page_context["video_time"],
            "duration": page_context["duration"],
            "paused": page_context["paused"],
            "page_title": page_context["page_title"],
            "page_url": page_context["page_url"],
            "has_video": page_context["has_video"],
        } if self._browser.connected else None

        # 自动附上页面结构，让 DS 知道当前页面有什么（非视频页用 AX tree+DOS 兜底）
        user_text = text
        if self._browser.connected:
            structures = []

            # 基本信息（始终附上）
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
                    elements = []
                    # 先试 AX tree
                    ax = self._run_async(self._browser.get_page_ax_tree(), timeout=5)
                    elements = ax.get("elements", [])
                    if not elements:
                        # AX tree 为空（如 Vue SPA 用 div+click 替代 a/button）→ 回退 DOM 扫描
                        self._gui.log("AX tree 为空，回退 DOM...", "dim")
                        dom = self._run_async(self._browser.get_page_structure(), timeout=10)
                        elements = dom.get("elements", [])

                    if elements:
                        links = [e for e in elements
                                 if e["type"] in ("link", "nav_link", "video_related", "content_link",
                                                  "menuitem", "tab", "listitem", "option")]
                        buttons = [e for e in elements if e["type"] == "button"]
                        headings = [e for e in elements if e["type"] == "heading"]

                        structures.append(f"[交互元素 {len(elements)}个]")
                        if links:
                            structures.append(
                                "链接: " + " | ".join(
                                    f"{l['text'][:25]}" + (f"→{l.get('href','')[:50]}" if l.get('href') else "")
                                    for l in links[:12]
                                ))
                        if buttons:
                            structures.append("按钮: " + ", ".join(b["text"][:20] for b in buttons[:6]))
                        if headings:
                            structures.append("章节: " + ", ".join(h["text"][:30] for h in headings[:6]))
                except Exception:
                    pass  # 获取失败不阻塞聊天

            if structures:
                user_text = text + "\n---\n当前页面：" + "; ".join(structures)

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
            self._gui.assistant_say("收到！")  # 兜底

        # 记录对话
        self._chat_history.append({"role": "user", "content": text})
        self._chat_history.append({"role": "assistant", "content": reply})

        # 限制历史长度
        if len(self._chat_history) > 40:
            self._chat_history = self._chat_history[-40:]

        # 执行动作
        if actions:
            self._gui.log(f"DS 返回 {len(actions)} 个动作: {[a.get('type') for a in actions]}", "dim")
            state_changed = self._execute_ds_actions(actions, text)

            if state_changed and self._browser.connected:
                # 页面变化后自动续问 DS 一轮，让它看新页面并回复用户
                self._gui.log("页面已变化，自动续问 DS...", "dim")
                self._auto_continue_after_page_change()

    def _execute_ds_actions(self, actions: list[dict], user_text: str = "") -> bool:
        """执行 DeepSeek 返回的动作指令。

        get_page 动作会触发二次 DS 调用：拿到页面结构后回传给 DS 分析跳转入口。

        Returns:
            True:  页面状态已改变（navigate/search → 调用方应续问 DS 一轮）
            False: 页面未变或已内部处理完毕
        """
        if not self._browser.connected or not self._browser.has_video_page:
            for a in actions:
                t = a.get("type", "")
                if t in ("analyze", "seek", "pause", "play", "screenshot"):
                    self._gui.assistant_say("请先连接浏览器。输入「帮我分析」自动处理。")
                    return False

        needs_page_roundtrip = False
        state_changed = False

        for act in actions:
            t = act.get("type", "")
            try:
                if t == "get_page":
                    # DS 请求查看当前页面结构 — 标记需要回传
                    needs_page_roundtrip = True
                    break  # 先处理 get_page，其他动作等二次决策

                elif t == "new_session":
                    # DS 决定开启新分析会话
                    self._gui.log("DS 请求新会话", "accent")
                    self._session_dir = ""
                    self._stop_requested = True
                    self._analyzing = False
                    self._gui.assistant_say("正在创建新会话...")
                    self._setup_session_folder()
                    if not self._caption_running:
                        self.start_caption(save_dir=self._session_dir)
                    self.start_analysis()
                    return False

                elif t == "seek":
                    target = float(act.get("time", 0))
                    self._run_async(self._browser.seek(target))
                    self._gui.log(f"  ⏩ seek {target:.0f}s", "dim")
                    self._last_video_time = target

                elif t == "pause":
                    self._run_async(self._browser.pause())
                    self._gui.log("  ⏸ pause", "dim")

                elif t == "play":
                    self._run_async(self._browser.play())
                    self._gui.log("  ▶ play", "dim")

                elif t == "screenshot":
                    name = act.get("name", "截图")
                    self._screenshot_at_current(name)

                elif t == "click":
                    click_text = act.get("text", "").strip()
                    click_selector = act.get("selector", "").strip()
                    click_index = int(act.get("index", 0))
                    if click_text or click_selector:
                        self._gui.log(
                            f"  🖱 click "
                            + (f"'{click_text[:30]}'" if click_text else f"selector='{click_selector[:40]}'")
                            + (f" idx={click_index}" if click_index > 0 else ""),
                            "dim",
                        )
                        state_changed = True  # 点击可能改变页面内容
                        ok = self._run_async(
                            self._browser.click_element(
                                text=click_text or None,
                                selector=click_selector or None,
                                index=click_index,
                            ),
                            timeout=10,
                        )
                        if ok:
                            self._gui.assistant_say(
                                f"已点击「{click_text[:30] if click_text else click_selector[:30]}」"
                            )
                        else:
                            self._gui.log("点击失败（未找到元素）", "warn")

                elif t == "navigate":
                    url = act.get("url", "")
                    if url:
                        self._gui.log(f"  导航: {url}", "dim")
                        state_changed = True
                        self._run_async(self._browser.navigate(url))
                        self._gui.assistant_say(f"已打开 {url}")
                        # 标记到历史，下次 DS 知道已导航过此地
                        self._chat_history.append({
                            "role": "system",
                            "content": f"（系统已导航到 {url}，当前页面即此地址。用户接下来问的是这个页面上的问题，不要重复导航。）"
                        })

                elif t == "search":
                    query = act.get("query", "")
                    if query:
                        search_url = f"https://www.bilibili.com/search?keyword={query}"
                        self._gui.log(f"  搜索: {query}", "dim")
                        state_changed = True
                        self._run_async(self._browser.navigate(search_url))
                        self._gui.assistant_say(f"已搜索: {query}")
                        self._chat_history.append({
                            "role": "system",
                            "content": f"（系统已搜索 {query}，当前在搜索结果页。用户接下来问的是结果页上的问题，不要重复搜索。）"
                        })

                elif t == "analyze":
                    # DS 已决策要分析 → 直接设置会话并启动
                    if not self._session_dir:
                        self._setup_session_folder()
                    if not self._caption_running:
                        self.start_caption(save_dir=self._session_dir)
                    self.start_analysis()
                    return False  # analyze 是完整流程，后面的动作没必要了

                elif t == "status":
                    self._show_status()

            except Exception as e:
                self._gui.log(f"  动作 {t} 失败: {e}", "warn")

        # ── get_page 二次决策：拿到页面结构 → 回传给 DS 分析跳转入口 ──
        if needs_page_roundtrip:
            self._gui.log("DS 请求页面结构，正在提取（AX Tree 优先）...", "accent")
            try:
                # 优先用 AX tree（更通用，不依赖 CSS 选择器）
                page_data = self._run_async(self._browser.get_page_ax_tree(), timeout=10)
                if not page_data.get("elements"):
                    # AX tree 为空 → 回退到 DOM 扫描
                    self._gui.log("AX tree 为空，回退到 DOM 扫描...", "dim")
                    page_data = self._run_async(self._browser.get_page_structure())
            except Exception:
                # AX tree 失败 → 回退
                self._gui.log("AX tree 提取失败，回退到 DOM 扫描...", "dim")
                page_data = self._run_async(self._browser.get_page_structure())

            elements = page_data.get("elements", [])
            page_title = page_data.get("title", "")
            page_url = page_data.get("url", "")
            page_source = page_data.get("source", "dom")

            # 构建结构化摘要给 DS（兼容 AX tree 和 DOM 两种格式）
            links = [e for e in elements if e["type"] in ("link", "nav_link", "video_related", "content_link")]
            buttons = [e for e in elements if e["type"] == "button"]
            headings = [e for e in elements if e["type"] == "heading"]
            search_boxes = [e for e in elements if e["type"] == "search_box"]

            page_summary_parts = [f"当前页面: {page_title}", f"URL: {page_url}"]
            page_summary_parts.append(f"\n可见交互元素共 {len(elements)} 个 (来源: {page_source}):\n")
            if links:
                page_summary_parts.append(f"【链接】({len(links)}个):")
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

            self._gui.log(f"页面结构: {len(elements)} 个元素 (AX={page_source}) -> 回传 DS 分析", "dim")

            # 回传 DS 做二次决策
            follow_msg = f"请分析以下页面结构，用户原话「{user_text}」。找到最匹配的链接并导航：\n\n{page_summary}"
            self._chat_history.append({"role": "user", "content": follow_msg})

            try:
                result = self._deepseek.chat(
                    user_message=follow_msg,
                    conversation_history=self._chat_history,
                )
            except Exception as e:
                self._gui.assistant_say(f"DS 二次调用失败: {e}")
                return False

            reply = result.get("reply", "")
            follow_actions = result.get("actions", [])

            if reply:
                self._gui.assistant_say(reply)
            self._chat_history.append({"role": "assistant", "content": reply})

            if follow_actions:
                self._gui.log(f"DS 二次返回 {len(follow_actions)} 个动作: {[a.get('type') for a in follow_actions]}", "dim")
                self._execute_ds_actions(follow_actions, user_text)

            return False  # get_page 已内部处理完成

        return state_changed  # navigate/search → 调用方续问 DS

    # ── 自动续问：navigate/search 后自动让 DS 看新页面 ──

    def _auto_continue_after_page_change(self):
        """navigate/search 执行后，自动获取新页面上下文 + 问 DS 一轮。

        解决「AI navigate 完后就没下文了」的问题——页面变了，
        DS 应该继续看新页面、回复用户、或发出下一步动作。
        最多执行 1 次续问，防止无限循环。
        """
        # 收集新页面上下文（复用 _handle_ds_chat 的逻辑）
        structures = []
        try:
            state = self._run_async(self._browser.get_state(), timeout=5)
            if state:
                if state.get("page_title"):
                    structures.append(f"[页面] {state['page_title'][:60]}")
                if state.get("page_url"):
                    structures.append(f"[URL] {state['page_url'][:80]}")
        except Exception:
            pass

        # 非视频页 → 附页面元素
        if not (state and state.get("has_video")):
            try:
                elements = []
                ax = self._run_async(self._browser.get_page_ax_tree(), timeout=5)
                elements = ax.get("elements", [])
                if not elements:
                    dom = self._run_async(self._browser.get_page_structure(), timeout=10)
                    elements = dom.get("elements", [])

                if elements:
                    links = [e for e in elements
                             if e["type"] in ("link", "nav_link", "video_related", "content_link",
                                              "menuitem", "tab", "listitem", "option")]
                    buttons = [e for e in elements if e["type"] == "button"]
                    structures.append(f"[交互元素 {len(elements)}个]")
                    if links:
                        structures.append("链接: " + " | ".join(
                            f"{l['text'][:25]}" + (f"→{l.get('href','')[:50]}" if l.get('href') else "")
                            for l in links[:8]
                        ))
                    if buttons:
                        structures.append("按钮: " + ", ".join(b["text"][:15] for b in buttons[:5]))
            except Exception:
                pass

        page_desc = "; ".join(structures) if structures else "（无法获取页面信息）"

        follow_msg = (
            f"上一轮操作已完成。当前页面内容:\n{page_desc}\n\n"
            "请基于这些信息给用户一个自然的回复"
            "（描述页面内容/问用户想看什么/如果需要进一步操作则附加 JSON 动作）。"
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
            self._gui.log(f"续问返回 {len(follow_actions)} 动作: {[a.get('type') for a in follow_actions]}", "dim")
            # 不再自动续问（防无限循环）
            self._execute_ds_actions(follow_actions, "")

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

    def _screenshot_at_current(self, name: str):
        """在当前时间点截图并命名。"""
        if not self._browser.has_video_page:
            self._gui.assistant_say("未连接到视频页面。")
            return
        try:
            t = self._run_async(self._browser.get_current_time())
            self._last_video_time = t
            # 确保有保存目录
            if not self._screenshot_dir:
                self._screenshot_dir = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "screenshots"
                )
            os.makedirs(self._screenshot_dir, exist_ok=True)
            minutes = int(t // 60)
            seconds = int(t % 60)
            safe_name = re.sub(r'[\\/:*?"<>|]', '_', name).strip().replace(" ", "_")[:30]
            fname = f"{minutes:02d}{seconds:02d}s_{safe_name}.png"
            path = os.path.join(self._screenshot_dir, fname)
            self._run_async(self._browser.screenshot(path))
            size_kb = os.path.getsize(path) // 1024
            self._gui.log(f"  📸 {fname} ({size_kb}KB)", "success")
            self._gui.assistant_say(f"📸 已截图: {fname}")
        except Exception as e:
            self._gui.log(f"  截图失败: {e}", "warn")
            self._gui.assistant_say(f"截图失败: {e}")

    def _show_status(self):
        """在聊天中显示当前状态。"""
        lines = [
            f"浏览器: {'已连接' if self._browser.connected else '未连接'}",
            f"视频页: {'已定位' if self._browser.has_video_page else '未定位'}",
            f"字幕: {'录制中' if self._caption_running else '未启动'}",
            f"分析: {'运行中' if self._analyzing else '空闲'}",
        ]
        if self._video_duration > 0:
            lines.append(f"进度: {self._last_video_time:.0f}s / {self._video_duration:.0f}s")
        self._gui.assistant_say("\n".join(lines))

    def _get_safe_video_title(self) -> str:
        """从当前视频页面获取安全的文件夹名。"""
        try:
            state = self._run_async(self._browser.get_state(), timeout=5)
            if state:
                title = state.get("page_title", "")
                if title:
                    # 去掉常见后缀和特殊字符
                    title = re.sub(r'\s*[-|—–]\s*.*?(?:Bilibili|YouTube|bilibili|哔哩).*$', '', title)
                    title = re.sub(r'[\\/:*?"<>|]', '_', title)  # Windows 非法字符
                    title = re.sub(r'\s+', '_', title.strip())
                    title = title[:40]  # 限制长度
                    return title
        except Exception:
            pass
        return ""

    # ── 启动 Agent（只连浏览器，不做任何决策）──

    def _start_agent(self):
        """启动 Agent：只连接浏览器 + 锁定用户可见标签页。不分析、不决策。

        不走 connect_browser()（它会调用 _find_video_page 找 <video> 标签，
        非视频页会导致 self._page 被设为 None）。
        """
        self._gui.log("启动 Agent...", "accent")

        if not self._browser.connected:
            self._gui.assistant_say("正在连接浏览器...")
            self._start_async_loop()
            try:
                ok = self._run_async(self._browser.connect(auto_start=True), timeout=120)
            except Exception as e:
                self._gui.log(f"浏览器连接异常: {e}", "error")
                self._gui.assistant_say("浏览器连接超时。请关闭所有 Edge 窗口后重试。")
                return False
            if not ok:
                self._gui.assistant_say(
                    "未检测到 Edge 调试模式。\n请关闭 Edge，用桌面「Edge (调试模式)」快捷方式重开。"
                )
                return False
            self._gui.assistant_say("浏览器已连接 ✓")

        # 锁定用户正在看的标签页（不找视频页）
        self._run_async(self._browser.ensure_active_tab())
        page_title = ""
        try:
            state = self._run_async(self._browser.get_state(), timeout=5)
            page_title = state.get("page_title", "")
        except Exception:
            pass
        self._gui.log(
            f"已锁定标签页: {page_title[:40] if page_title else '(未知)'}", "dim"
        )
        self._gui.set_status({"connected": True})
        self._gui.assistant_say(
            "Agent 已就绪！可以跟我说「帮我分析」「帮我找视频」或任何你想做的事。"
        )
        return True

    # ── 会话文件夹 ──

    def _setup_session_folder(self):
        """创建会话文件夹（按视频标题+时间戳命名）。如果已有会话则复用。"""
        if self._session_dir and os.path.isdir(self._session_dir):
            self._gui.log(f"复用会话: {os.path.basename(self._session_dir)}/", "dim")
            return

        safe_title = self._get_safe_video_title()
        session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder_name = f"{session_ts}_{safe_title}" if safe_title else session_ts
        self._session_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "sessions", folder_name
        )
        self._screenshot_dir = os.path.join(self._session_dir, "screenshots")
        os.makedirs(self._screenshot_dir, exist_ok=True)
        self._gui.log(f"会话文件夹: sessions/{folder_name}/", "accent")

    # ── 手动截图 ──

    def _manual_screenshot(self):
        if not self._browser.has_video_page:
            self._gui.assistant_say("请先连接浏览器。")
            return
        try:
            t = self._run_async(self._browser.get_current_time())
            path = os.path.join(self._screenshot_dir, f"manual_{int(t)}s.png")
            self._run_async(self._browser.screenshot(path))
            self._gui.log(f"手动截图: {os.path.basename(path)}", "success")
            self._gui.assistant_say(f"📸 已截图: {int(t)}s")
        except Exception as e:
            self._gui.log(f"手动截图失败: {e}", "error")

    # ═══════════════════════════════════════════════════════
    #  清理
    # ═══════════════════════════════════════════════════════

    def shutdown(self):
        """安全关闭所有组件。"""
        self._gui.log("正在关闭...", "accent")
        self.stop_analysis()
        self.stop_caption()
        if self._browser.connected:
            try:
                self._run_async(self._browser.disconnect())
            except Exception:
                pass
        self._stop_async_loop()
        self._gui.log("VideoAgent 已关闭", "dim")

    def _scan_player(self):
        """扫描当前页面的媒体播放器，LLM 辅助识别。"""
        if not self._browser.connected or not self._browser.has_video_page:
            self._gui.assistant_say("请先连接浏览器并打开视频页面。")
            return

        self._gui.log("扫描页面媒体元素...", "accent")
        try:
            scan = self._run_async(self._browser.scan_page_for_media(), timeout=10)
        except Exception as e:
            self._gui.assistant_say(f"扫描失败: {e}")
            return

        if "error" in scan:
            self._gui.assistant_say(f"扫描出错: {scan['error']}")
            return

        # 构建给 LLM 的提示
        summary = f"页面: {scan.get('title', '?')}\n"
        summary += f"URL: {scan.get('url', '?')}\n"
        summary += f"当前选择器: {scan.get('selector', 'video')}\n\n"

        videos = scan.get("videos", [])
        if videos:
            summary += f"### 标准媒体标签 ({len(videos)} 个)\n"
            for v in videos:
                summary += f"- <{v['tag']}> id={v['id']!r} class={v['className']!r}"
                summary += f" {v['width']}x{v['height']}"
                if v.get('duration'):
                    summary += f" 时长={v['duration']:.0f}s"
                summary += "\n"

        custom = scan.get("custom_players", [])
        if custom:
            summary += f"\n### 自定义播放器 ({len(custom)} 个)\n"
            for p in custom:
                summary += f"- {p['selector']} ({p['tagName']}) {p['width']}x{p['height']}"
                if p.get('hasVideoInside'):
                    summary += f" [内部有video: {p['innerVideoSelector']}]"
                summary += "\n"

        iframes = scan.get("iframes", [])
        if iframes:
            summary += f"\n### 大尺寸 iframe ({len(iframes)} 个)\n"
            for f in iframes[:5]:
                summary += f"- {f['src'][:80]} {f['width']}x{f['height']}\n"

        self._gui.log(summary, "dim")
        self._gui.assistant_say(f"📊 页面扫描完成：\n{summary[:1000]}")

        # 如果有建议的选择器，自动应用
        suggestion = scan.get("suggestion", "video")
        if suggestion != "video" and suggestion != scan.get("selector", "video"):
            self._browser.set_video_selector(suggestion)
            self._gui.assistant_say(f"✅ 已自动切换选择器: `{suggestion}`")


# 类型引用（避免循环导入）
from agent_gui import AgentGUI
