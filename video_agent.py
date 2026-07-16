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

        # 状态
        self._last_video_time = 0.0
        self._video_duration = 0.0

    # ═══════════════════════════════════════════════════════
    #  asyncio 桥接（浏览器操作在线程中运行）
    # ═══════════════════════════════════════════════════════

    def _start_async_loop(self):
        """启动 asyncio 事件循环线程（供浏览器操作使用）。"""
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

    def connect_browser(self) -> bool:
        """连接 Edge CDP 并自动定位视频页面。"""
        self._gui.log("正在连接浏览器...", "accent")
        self._start_async_loop()

        ok = self._run_async(self._browser.connect(auto_start=True))
        if not ok:
            self._gui.log("浏览器连接失败", "error")
            self._gui.system_say("❌ 浏览器连接失败，请确认 Edge 已安装")
            self._gui.set_status({"connected": False})
            return False

        self._gui.log("CDP 已连接", "success")

        # 列出页面
        pages = self._run_async(self._browser.list_pages())
        self._gui.log(f"发现 {len(pages)} 个标签页:")
        for i, p in enumerate(pages):
            icon = "▶" if p["has_video"] else " "
            self._gui.log(f"  [{i}] {icon} {p['title'][:40]}")
            self._gui.log(f"      {p['url'][:60]}", "dim")

        # 自动定位视频页
        found = self._run_async(self._browser.select_video_page())
        if not found:
            self._gui.log("未找到含视频的标签页，请在 Edge 中打开视频后重试", "warn")
            self._gui.system_say("⚠ 未检测到视频标签页。请在 Edge 中打开要分析的视频。")
            self._gui.set_status({"connected": True, "caption_running": False})
            return False

        # 获取视频信息
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
        else:
            self._gui.log("已定位视频页，无法读取视频状态", "warn")

        self._gui.set_status({
            "connected": True,
            "caption_running": False,
            "video_time": self._last_video_time,
            "duration": self._video_duration,
        })
        self._gui.log("浏览器就绪 ✓", "success")
        return True

    # ═══════════════════════════════════════════════════════
    #  转录
    # ═══════════════════════════════════════════════════════

    def start_caption(self, show_gui: bool = False) -> bool:
        """启动 Whisper 转录。"""
        if not HAS_TRANSCRIBER:
            self._gui.log("转录模块未安装，请检查 transcriber_core.py", "error")
            return False

        if not check_transcriber_deps():
            self._gui.log("转录依赖缺失，请安装 soundcard, faster-whisper", "error")
            return False

        import queue as qmod

        script_dir = os.path.dirname(os.path.abspath(__file__))
        self._screenshot_dir = os.path.join(script_dir, "screenshots")
        os.makedirs(self._screenshot_dir, exist_ok=True)

        # 日志路径（和原来一致: captions_YYYYMMDD.txt）
        self._caption_log_path = os.path.join(
            script_dir, f"captions_{datetime.now().strftime('%Y%m%d')}.txt"
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
        """读取字幕日志，返回最近 N 行（过滤掉标题和分隔符）。"""
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

        流程:
          1. 等待字幕积累一段时间
          2. 读取最近字幕 → DeepSeek 分析 → 批量截图
          3. 重复
        """
        if not self._browser.has_video_page:
            self._gui.log("未连接视频页，分析中止", "error")
            self._analyzing = False
            return

        # 先等字幕积累 30 秒
        self._gui.log("等待字幕积累 (30s)...", "dim")
        for _ in range(30):
            if self._stop_requested:
                self._analyzing = False
                return
            time.sleep(1)

        round_num = 0
        while not self._stop_requested and self._analyzing:
            round_num += 1
            self._gui.log(f"--- 分析轮次 {round_num} ---", "accent")

            # 1. 更新视频状态
            try:
                state = self._run_async(self._browser.get_state())
                if state:
                    self._last_video_time = state["current_time"]
                    self._video_duration = state.get("duration", 0)
                self._gui.set_status({
                    "connected": True,
                    "caption_running": self._caption_running,
                    "video_time": self._last_video_time,
                    "duration": self._video_duration,
                })
            except Exception as e:
                self._gui.log(f"读取视频状态失败: {e}", "error")

            # 2. 读字幕
            subtitle = self._read_caption_log(tail=80)
            if not subtitle.strip():
                self._gui.log("字幕为空，等待中...", "dim")
                time.sleep(10)
                continue

            line_count = subtitle.count("\n") + 1
            self._gui.log(f"字幕: {line_count} 行", "dim")

            # 3. DeepSeek 分析
            try:
                self._gui.log("DeepSeek 分析中...", "accent")
                shots = self._deepseek.analyze_screenshots(subtitle)
                self._gui.log(
                    f"DeepSeek 生成 {len(shots)} 个截图点: {[s['name'] for s in shots]}",
                    "success",
                )
                self._gui.assistant_say(
                    f"本段字幕分析结果：\n" +
                    "\n".join(f"  • {s['time']:.0f}s — {s['name']}" for s in shots)
                )
            except Exception as e:
                self._gui.log(f"DeepSeek 分析失败: {e}", "error")
                time.sleep(10)
                continue

            if not shots:
                self._gui.log("无截图点，跳过", "dim")
                time.sleep(15)
                continue

            # 4. 批量截图
            try:
                self._gui.log(f"批量截图 {len(shots)} 张...", "accent")
                results = self._run_async(
                    self._browser.capture_batch(shots, save_dir=self._screenshot_dir)
                )
                for r in results:
                    fname = os.path.basename(r["path"])
                    self._gui.log(f"  ✓ {fname}", "success")
            except Exception as e:
                self._gui.log(f"截图失败: {e}", "error")

            # 5. 等 30 秒再分析下一段
            self._gui.log(f"等待 30s 后分析下一段...", "dim")
            for _ in range(30):
                if self._stop_requested:
                    break
                time.sleep(1)

        self._analyzing = False
        self._gui.log("分析循环已结束", "accent")
        self._gui.system_say("✅ 分析完成！")

    # ═══════════════════════════════════════════════════════
    #  用户指令处理（来自 GUI 聊天框）
    # ═══════════════════════════════════════════════════════

    def handle_user_message(self, text: str):
        """处理用户在聊天框输入的指令。"""
        text_lower = text.strip().lower()
        text_raw = text.strip()

        # ── 一键启动：帮我分析 ──
        if any(kw in text_raw for kw in ("帮我分析", "分析这个视频", "分析视频", "分析一下")):
            self._one_click_analyze()
            return

        # ── 设置密钥 ──
        if text_raw.startswith("设置密钥") or text_raw.startswith("设置 key"):
            parts = text_raw.split(None, 1)
            if len(parts) >= 2:
                key = parts[1].strip()
                if key:
                    self._deepseek = DeepSeekClient(api_key=key)
                    self._gui.log("API Key 已更新 ✓", "success")
                    self._gui.assistant_say("✅ API Key 已更新！现在可以开始分析了。")
                    return
            self._gui.assistant_say("格式: 设置密钥 sk-xxxxxxxx")
            return

        # ── 启动分析 ──
        if text_lower in ("开始分析", "start", "分析", "go"):
            if not self._browser.connected or not self._browser.has_video_page:
                self._gui.assistant_say("请先连接浏览器并打开视频页面。可以输入「连接浏览器」或直接说「帮我分析这个视频」。")
                return
            if not self._caption_running:
                self._gui.assistant_say("字幕录制尚未开始。正在启动...")
                self.start_caption()
            self.start_analysis()

        # ── 停止 ──
        elif text_lower in ("停止", "stop", "停止分析"):
            self.stop_analysis()
            self._gui.assistant_say("分析已停止。")

        # ── 连接浏览器 ──
        elif text_lower in ("连接浏览器", "connect", "连接"):
            if self._browser.connected:
                self._gui.assistant_say("浏览器已连接。输入「帮我分析」开始自动分析。")
            else:
                ok = self.connect_browser()
                if ok:
                    self._gui.assistant_say("✅ 浏览器连接成功！输入「帮我分析」开始分析。")

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
                "  • 帮我分析这个视频 — 一键启动\n"
                "  • 连接浏览器 — 单独连接\n"
                "  • 开始分析 — 仅启动分析\n"
                "  • 停止 — 停止分析\n"
                "  • 截图 — 手动截图当前帧\n"
                "  • 状态 — 查看当前状态\n"
                "  • 设置密钥 sk-xxx — 配置 API Key\n\n"
                "也可以直接输入自然语言指令，如「多截一些代码相关的图」"
            )

        # ── 自然语言自定义分析 ──
        else:
            if not self._caption_running:
                self._gui.assistant_say("字幕尚未启动。输入「帮我分析」启动完整流程。")
                return
            subtitle = self._read_caption_log(tail=80)
            if not subtitle.strip():
                self._gui.assistant_say("暂无字幕数据，请等待...")
                return
            try:
                self._gui.log(f"自定义分析: {text_raw}", "accent")
                shots = self._deepseek.analyze_screenshots(subtitle, custom_instruction=text_raw)
                self._gui.assistant_say(
                    f"根据你的要求分析结果：\n" +
                    "\n".join(f"  • {s['time']:.0f}s — {s['name']}" for s in shots)
                )
            except Exception as e:
                self._gui.assistant_say(f"分析失败: {e}")

    # ── 一键分析 ──

    def _one_click_analyze(self):
        """一键启动完整流程：连接浏览器 → 启动字幕 → 开始分析。"""
        if not self._deepseek.configured:
            self._gui.assistant_say(
                "⚠ 尚未配置 DeepSeek API Key。\n"
                "请在「配置」面板（标题栏 ⚙）中设置，或输入「设置密钥 sk-xxx」"
            )
            return

        self._gui.assistant_say("🤖 收到！正在启动分析流程...")
        self._gui.log("「帮我分析」一键启动", "accent")

        # Step 1: 连接浏览器
        if not self._browser.connected:
            self._gui.assistant_say("第一步：连接浏览器...")
            ok = self.connect_browser()
            if not ok:
                self._gui.assistant_say(
                    "❌ 浏览器连接失败。\n请确保 Edge 以调试模式运行后输入「连接浏览器」重试。"
                )
                return

        # Step 2: 启动字幕
        if not self._caption_running:
            self._gui.assistant_say("第二步：启动字幕录制...")
            ok = self.start_caption()
            if not ok:
                self._gui.assistant_say("❌ 字幕启动失败，请检查依赖。")
                return

        # Step 3: 开始分析
        self._gui.assistant_say("第三步：开始自动分析！🎬")
        self.start_analysis()

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


# 类型引用（避免循环导入）
from agent_gui import AgentGUI
