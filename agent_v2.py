#!/usr/bin/env python3
"""
VideoAgent v2 — 桌面悬浮 AI 视频分析助手（聊天驱动版）

用法:
  python agent_v2.py          自动选择模式（GUI 优先，失败则 CLI）
  python agent_v2.py --gui    强制 GUI 悬浮窗
  python agent_v2.py --cli    命令行聊天模式

前提:
  1. 安装依赖: playwright, requests, soundcard, faster-whisper, numpy
  2. 在 Edge 中打开要分析的视频（建议加 --remote-debugging-port=9222）

交互方式:
  "帮我分析这个视频" → 一键连接浏览器 + 启动字幕 + 开始分析
  "连接浏览器"       → 单独连接 Edge CDP
  "停止"             → 暂停分析
  "设置密钥 sk-xxx"   → 动态设置 DeepSeek API Key
  "状态"             → 查看当前状态
  "截图"             → 手动截图当前帧

输出:
  captions_YYYYMMDD.txt   — 完整字幕（带时间戳）
  screenshots/            — 语义命名截图
"""

import os
import sys
import threading
import time
from collections.abc import Callable

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ─────────────────────────────────────────────────────────
#  命令行版 GUI（实现和 AgentGUI 一样的接口，但走终端）
# ─────────────────────────────────────────────────────────

class CmdlineGUI:
    """命令行聊天下实现 AgentGUI 接口，在终端模拟悬浮窗。"""

    def __init__(self, **kwargs):
        self._on_user_message = kwargs.get("on_user_message")
        self._on_close = kwargs.get("on_close")
        self._on_config_save = kwargs.get("on_config_save")
        self._get_api_key = kwargs.get("get_api_key")
        self._on_ready = None
        self._running = False
        self._api_key = kwargs.get("api_key", "")

    # ── 公开方法（兼容 AgentGUI） ──

    def _safe_print(self, s: str):
        try:
            print(s)
        except UnicodeEncodeError:
            # Windows GBK 终端不支持 emoji，去掉特殊字符
            import re
            print(re.sub(r'[^\u0020-\u7fff\u3000-\u303f\uff00-\uffef]', '?', s))

    def log(self, msg: str, tag: str = "dim", with_time: bool = True):
        self._safe_print(f"  [{msg}]")

    def assistant_say(self, msg: str):
        for line in msg.split("\n"):
            self._safe_print(f"  AI: {line}")

    def system_say(self, msg: str):
        for line in msg.split("\n"):
            self._safe_print(f"  >> {line}")

    def set_status(self, status: dict):
        parts = []
        if status.get("analyzing"):
            parts.append("分析中")
        elif status.get("connected"):
            parts.append("已连接")
        else:
            parts.append("待机")
        if status.get("caption_running"):
            parts.append(f"字幕 {status['caption_time']:.0f}s")
        v, d = status.get("video_time", 0), status.get("duration", 0)
        if d > 0:
            parts.append(f"{v:.0f}/{d:.0f}s")
        self._safe_print(f"  -- {' | '.join(parts)} --")

    def set_title(self, title: str):
        self._safe_print(f"[{title}]")

    def start(self):
        self._running = True
        self._safe_print("  [输入「帮助」查看指令 | 输入「退出」或 Ctrl+C 退出]")
        if self._on_ready:
            self._on_ready()
        self._input_loop()

    def stop(self):
        self._running = False

    # ── 输入循环 ──

    def _input_loop(self):
        while self._running:
            try:
                text = input("  > ").strip()
                if not text:
                    continue
                if text.lower() in ("退出", "exit", "quit", "q"):
                    if self._on_close:
                        self._on_close()
                    break
                if self._on_user_message:
                    self._on_user_message(text)
            except (KeyboardInterrupt, EOFError):
                self._safe_print("\n  再见")
                if self._on_close:
                    self._on_close()
                break

# ─────────────────────────────────────────────────────────
#  main
# ─────────────────────────────────────────────────────────

def _safe_print(s: str):
    """安全打印，过滤 Windows GBK 终端不支持的字符。"""
    try:
        print(s)
    except UnicodeEncodeError:
        import re
        print(re.sub(r'[^\u0020-\u007e\u3000-\u303f\uff00-\uffef]', '?', s))


def main():
    mode = "auto"
    if "--cli" in sys.argv:
        mode = "cli"
    elif "--gui" in sys.argv:
        mode = "gui"

    _safe_print("=" * 60)
    _safe_print("  VideoAgent v2 — 桌面悬浮 AI 视频分析助手")
    _safe_print("=" * 60)

    # ── 加载 .env 配置 ──
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(ROOT, ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)
            _safe_print("[配置] 已加载 .env")
        else:
            _safe_print("[配置] .env 不存在，请复制 .env.example 并填入 API Key")
    except ImportError:
        pass

    # ── 延迟导入 ──
    from deepseek_client import DeepSeekClient
    from video_agent import VideoAgent

    # ── API Key ──
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    deepseek = DeepSeekClient(api_key=api_key)

    if deepseek.configured:
        _safe_print("[DeepSeek] API Key 已配置 ✓")
    else:
        _safe_print("[DeepSeek] 未配置 API Key，输入「设置密钥 sk-xxx」配置")

    agent: VideoAgent | None = None

    # ── 回调 ──

    def on_config_save(api_key_value: str):
        # delegate to VideoAgent so it saves to .env AND updates its client
        if agent:
            return agent.set_api_key(api_key_value)
        # fallback: agent not ready yet (shouldn't happen, but safe)
        nonlocal deepseek
        deepseek = DeepSeekClient(api_key=api_key_value.strip())
        gui.log("API Key 已更新（内存）", "warn")
        return True

    def get_current_api_key() -> str:
        return deepseek._api_key

    def on_user_message(text: str):
        if agent:
            threading.Thread(target=agent.handle_user_message, args=(text,), daemon=True).start()

    def on_close():
        _safe_print("\n[系统] 关闭...")
        if agent:
            agent.shutdown()

    # ── 选择 GUI 还是 CLI ──
    use_gui = False

    if mode == "gui":
        use_gui = True
    elif mode == "cli":
        use_gui = False
    else:
        # auto: 尝试 tkinter
        try:
            import tkinter
            import tkinter.ttk
            use_gui = True
        except (ImportError, tkinter.TclError):
            _safe_print("[模式] 无 GUI 环境，使用命令行聊天模式")
            use_gui = False

    # ── 构建界面 ──
    if use_gui:
        from agent_gui import AgentGUI
        _safe_print("[模式] GUI 悬浮窗")
        gui = AgentGUI(
            on_user_message=on_user_message,
            on_close=on_close,
            on_config_save=on_config_save,
            get_api_key=get_current_api_key,
        )

        def _welcome():
            api_status = "已配置" if deepseek.configured else "未配置"
            gui.system_say(
                f"欢迎使用 VideoAgent！\n\n"
                f"DeepSeek API: {api_status}\n"
                f"请在 Edge 中打开要分析的视频，然后输入「帮我分析」开始。\n\n"
                f"可用指令：\n"
                f"  • 帮我分析这个视频 — 一键启动\n"
                f"  • 连接浏览器 — 单独连接\n"
                f"  • 设置密钥 sk-xxx — 配置 API Key\n"
                f"  • 帮助 — 显示更多指令"
            )
            if not deepseek.configured:
                gui.system_say(
                    "⚠ 尚未配置 DeepSeek API Key。\n"
                    "请在「配置」面板中设置，或输入「设置密钥 sk-xxx」"
                )
        gui._on_ready = _welcome
    else:
        _safe_print("\n[模式] 命令行聊天 (--cli)")
        gui = CmdlineGUI(
            on_user_message=on_user_message,
            on_close=on_close,
            on_config_save=on_config_save,
            get_api_key=get_current_api_key,
        )

        def _welcome():
            api_status = "已配置" if deepseek.configured else "未配置"
            gui.system_say(
                f"欢迎使用 VideoAgent！\n\n"
                f"DeepSeek API: {api_status}\n"
                f"请在 Edge 中打开要分析的视频，然后输入「帮我分析」开始。"
            )
            if not deepseek.configured:
                gui.system_say(
                    "⚠ 尚未配置 DeepSeek API Key。输入「设置密钥 sk-xxx」配置"
                )
        gui._on_ready = _welcome

    # ── 构建 Agent ──
    agent = VideoAgent(gui, deepseek)

    # ── 启动 ──
    try:
        gui.start()
    except KeyboardInterrupt:
        pass
    finally:
        if agent:
            agent.shutdown()


if __name__ == "__main__":
    main()
