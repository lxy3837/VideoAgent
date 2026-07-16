"""
转录 MCP Server — 封装音频采集 + Whisper 识别为 MCP 工具

用法:
  python transcriber_mcp.py

工具:
  transcription_start  {show_gui: 0|1}
    show_gui=0: 静默后台转录，写日志文件
    show_gui=1: 启动 live_caption_video.py 子进程（带悬浮窗）
  transcription_read   {tail: 30}
    读取最近 N 行字幕
  transcription_stop   {}
    停止转录，返回完整字幕文本
"""

import sys
import os
import json
import time
import subprocess
import re
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── 导入转录核心 ──
from transcriber_core import (
    AudioCapture, Transcriber, check_dependencies,
    HAS_SOUNDCARD, HAS_WHISPER,
)

# ── 全局状态 ──
_capture = None      # AudioCapture 实例
_transcriber = None  # Transcriber 实例
_gui_proc = None     # GUI 子进程（show_gui=1 时）
_log_path = ""       # 字幕日志文件路径
_running = False

# ── 工具定义 ──
TOOLS = [
    {
        "name": "transcription_start",
        "description": """启动 Whisper 实时转录。
show_gui=0: 静默后台运行，只写日志文件
show_gui=1: 启动 live_caption_video.py 子进程，弹出悬浮字幕窗口""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "show_gui": {
                    "type": "integer",
                    "enum": [0, 1],
                    "default": 0,
                    "description": "0=静默模式, 1=显示悬浮字幕窗口"
                },
            },
            "required": [],
        },
    },
    {
        "name": "transcription_read",
        "description": """读取 Whisper 最新转录的字幕文本。

返回:
- transcript: 字幕文本，每行带本地时间戳 [T=XXs]
- line_count: 返回的行数
- log_path: 日志文件路径""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tail": {
                    "type": "integer",
                    "default": 50,
                    "description": "读取最近 N 行"
                },
            },
            "required": [],
        },
    },
    {
        "name": "transcription_stop",
        "description": """停止转录进程，返回完整字幕文本和日志文件路径。""",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _ensure_save_dir():
    """确保日志目录存在。"""
    save_dir = os.path.join(ROOT, "captions")
    os.makedirs(save_dir, exist_ok=True)
    return save_dir


def _make_log_path():
    return os.path.join(_ensure_save_dir(), f"captions_{time.strftime('%Y%m%d_%H%M%S')}.txt")


def _read_log(tail=50):
    """读字幕日志文件，返回 (行列表, 偏移量)。"""
    if not _log_path or not os.path.exists(_log_path):
        return [], 0
    with open(_log_path, "r", encoding="utf-8") as f:
        lines = [l.rstrip() for l in f.readlines()]
    # 过滤掉分隔线和标题行
    content = [l for l in lines if not l.startswith("===") and l != "---" and l.strip()]
    return content[-tail:] if len(content) > tail else content, len(content)


def _internal_start():
    """静默模式：直接在进程内启动 AudioCapture + Transcriber。"""
    global _capture, _transcriber, _log_path, _running
    import queue as qmod

    _log_path = _make_log_path()
    audio_q = qmod.Queue()

    def on_text(text):
        pass  # 静默模式，不输出到任何地方

    _capture = AudioCapture(audio_q)
    _transcriber = Transcriber(audio_q, on_text, save_path=_log_path)
    _capture.start()
    _transcriber.start()
    _running = True
    return {"status": "started", "mode": "headless", "log_path": _log_path}


def _gui_start():
    """GUI 模式：启动 live_caption_video.py 子进程。"""
    global _gui_proc, _log_path, _running

    script = os.path.join(ROOT, "live_caption_video.py")
    if not os.path.exists(script):
        return {"status": "error", "message": f"live_caption_video.py not found at {script}"}

    _gui_proc = subprocess.Popen(
        [sys.executable, script],
        cwd=ROOT,
    )
    _running = True

    # live_caption_video.py 会自己创建日志文件，格式是 captions_YYYYMMDD.txt
    _log_path = os.path.join(ROOT, f"captions_{time.strftime('%Y%m%d')}.txt")
    return {"status": "started", "mode": "gui", "pid": _gui_proc.pid, "log_path": _log_path}


def _internal_stop():
    """停止内部转录。"""
    global _capture, _transcriber, _running
    if _capture:
        _capture.stop()
        _capture = None
    if _transcriber:
        _transcriber.stop()
        _transcriber = None
    _running = False

    # 读日志
    lines, total = _read_log(tail=9999)
    return {
        "status": "stopped",
        "log_path": _log_path,
        "line_count": total,
        "transcript": "\n".join(lines),
    }


def _gui_stop():
    """停止 GUI 子进程。"""
    global _gui_proc, _running
    if _gui_proc and _gui_proc.poll() is None:
        _gui_proc.terminate()
        try:
            _gui_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _gui_proc.kill()
    _gui_proc = None
    _running = False

    # 读日志
    lines, total = _read_log(tail=9999)
    return {
        "status": "stopped",
        "log_path": _log_path,
        "line_count": total,
        "transcript": "\n".join(lines),
    }


# ═══════════════════════════════════════════════════════════
#  MCP JSON-RPC 处理
# ═══════════════════════════════════════════════════════════

def handle_request(req: dict) -> dict | None:
    global _running
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "transcriber-mcp", "version": "1.0"},
            }
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})

        if tool_name == "transcription_start":
            if _running:
                return {
                    "jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": json.dumps(
                        {"status": "already_running", "log_path": _log_path}
                    )}]}
                }

            if not check_dependencies():
                return {
                    "jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": json.dumps(
                        {"status": "error", "message": "依赖缺失，请 pip install soundcard faster-whisper"}
                    )}]}
                }

            show_gui = args.get("show_gui", 0)
            if show_gui:
                result = _gui_start()
            else:
                result = _internal_start()

            return {
                "jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            }

        if tool_name == "transcription_read":
            tail = args.get("tail", 50)
            lines, total = _read_log(tail)
            return {
                "jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": json.dumps({
                    "transcript": "\n".join(lines),
                    "line_count": len(lines),
                    "log_path": _log_path,
                    "running": _running,
                }, ensure_ascii=False)}]}
            }

        if tool_name == "transcription_stop":
            if not _running:
                return {
                    "jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": json.dumps(
                        {"status": "not_running"}
                    )}]}
                }

            if _gui_proc and _gui_proc.poll() is None:
                result = _gui_stop()
            else:
                result = _internal_stop()

            return {
                "jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            }

        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def main():
    """stdio JSON-RPC 主循环。"""
    print("[Transcriber MCP] 启动", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            continue
        except Exception as e:
            print(f"[Transcriber MCP] 错误: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    main()
