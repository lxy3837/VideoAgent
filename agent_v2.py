#!/usr/bin/env python3
"""
VideoAgent v2 — 桌面悬浮 AI 视频分析助手（聊天驱动版）

用法:
  python agent_v2.py

前提:
  1. 安装依赖: playwright, requests, soundcard, faster-whisper, numpy
  2. 在 Edge 中打开要分析的视频（建议加 --remote-debugging-port=9222）
  3. 启动后在聊天框输入指令

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

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    print("=" * 60)
    print("  VideoAgent v2 — 桌面悬浮 AI 视频分析助手")
    print("=" * 60)

    # ── 加载 .env 配置 ──
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(ROOT, ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)
            print("[配置] 已加载 .env")
        else:
            print("[配置] .env 不存在，请复制 .env.example 并填入 API Key")
    except ImportError:
        # python-dotenv 未安装，靠环境变量
        pass

    # ── 延迟导入 ──
    try:
        from agent_gui import AgentGUI
        from deepseek_client import DeepSeekClient
        from video_agent import VideoAgent
    except ImportError as e:
        print(f"[错误] 导入失败: {e}")
        input("按回车键退出...")
        return

    # ── API Key ──
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    deepseek = DeepSeekClient(api_key=api_key)

    if deepseek.configured:
        print("[DeepSeek] API Key 已配置 ✓")
    else:
        print("[DeepSeek] 未配置 API Key，可在 GUI 中设置或输入「设置密钥 sk-xxx」")

    # agent 变量
    agent: VideoAgent | None = None

    # ── GUI 回调 ──

    def on_config_save(api_key_value: str):
        """配置面板保存时触发。"""
        nonlocal deepseek
        if api_key_value.strip():
            deepseek = DeepSeekClient(api_key=api_key_value.strip())
            gui.log("API Key 已更新 ✓", "success")
            gui.set_title("VideoAgent (已配置)")
            return True
        return False

    def get_current_api_key() -> str:
        return deepseek._api_key

    def on_user_message(text: str):
        """用户聊天消息。"""
        if agent:
            threading.Thread(
                target=agent.handle_user_message,
                args=(text,),
                daemon=True,
            ).start()

    def on_close():
        print("\n[系统] 关闭...")
        if agent:
            agent.shutdown()

    # ── 构建 GUI ──
    gui = AgentGUI(
        on_user_message=on_user_message,
        on_close=on_close,
        on_config_save=on_config_save,
        get_api_key=get_current_api_key,
    )

    # ── 构建 Agent ──
    agent = VideoAgent(gui, deepseek)

    # ── 欢迎消息 ──
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
        # 如果没配 API Key，提示设置
        if not deepseek.configured:
            gui.system_say(
                "⚠ 尚未配置 DeepSeek API Key。\n"
                "请在「配置」面板中设置，或输入「设置密钥 sk-xxx」"
            )

    gui._on_ready = _welcome

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
