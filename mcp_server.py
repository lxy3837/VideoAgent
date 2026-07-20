"""
VideoAgent MCP Server — 将所有 Agent 能力封装为 MCP 工具。

架构:
  agent (video_agent.py) → MCP Server (stdio JSON-RPC) → browser_controller + session_manager + transcriber

启动:
  python mcp_server.py          # stdio 模式，供 agent 作为子进程调用
  python mcp_server.py --port N # HTTP 模式（调试用）

工具列表:
  session_list        — 列出所有会话
  session_create      — 创建新会话（可继承旧会话）
  session_switch      — 切换到已有会话
  session_status      — 当前会话详情
  browser_navigate    — 导航到 URL
  browser_seek        — 视频跳转
  browser_pause       — 暂停
  browser_play        — 播放
  browser_screenshot  — 截图（归入当前会话）
  browser_get_page    — 获取页面元素
  browser_click       — 点击页面元素
  browser_status      — 视频/页面状态
  transcription_start — 启动转录
  transcription_read  — 读取字幕
  transcription_stop  — 停止转录
  analysis_start      — 启动分析循环
  analysis_stop       — 停止分析
  analysis_status     — 分析状态
  ds_decide           — 让 DS 分析字幕并决定截图动作
"""

from __future__ import annotations

import sys
import os
import json
import time
import threading
import asyncio
import re
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── 导入现有模块（轻量模块立即导入，重量模块延迟加载）──
from session_manager import (
    list_sessions, create_session, get_session_info,
    get_screenshot_dir, update_session_meta, delete_session,
)
from deepseek_client import DeepSeekClient

# 浏览器（轻量导入 — mcp_server.py 本身没有重量依赖）
try:
    from browser_controller import BrowserController
    HAS_BROWSER = True
except ImportError:
    HAS_BROWSER = False

# 转录（延迟导入 — soundcard/faster-whisper 启动很慢，
# 等到 transcription_start 真正调用时才 import，避免阻塞 MCP 握手 initialize）
HAS_TRANSCRIBER = None  # None = 未检测，True/False = 已检测结果
_transcriber_module = None

def _ensure_transcriber():
    """延迟检查并导入转录模块。只在首次 transcription_start 时触发。"""
    global HAS_TRANSCRIBER, _transcriber_module
    if HAS_TRANSCRIBER is not None:
        return HAS_TRANSCRIBER
    try:
        from transcriber_core import (
            AudioCapture, Transcriber, check_dependencies as check_transcriber_deps,
        )
        _transcriber_module = (AudioCapture, Transcriber, check_transcriber_deps)
        HAS_TRANSCRIBER = True
    except ImportError:
        HAS_TRANSCRIBER = False
    return HAS_TRANSCRIBER


# ═══════════════════════════════════════════════════════════
#  MCP 工具定义
# ═══════════════════════════════════════════════════════════

TOOLS = [
    # ── 会话管理 ──
    {
        "name": "session_list",
        "description": """列出所有历史会话，包含名称、标题、创建时间、截图数量。

返回示例:
  {"sessions": [{"name": "20260719_1135_ROS2入门", "title": "ROS2入门21讲",
    "created_at": "2026-07-19T11:35:20", "screenshot_count": 24}, ...], "count": 5}

用途: DS 决策"是否继承已有会话"时的参考依据。""",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "session_create",
        "description": """创建新的会话文件夹。自动以 时间戳_名称 命名。

参数:
  name:        会话名称（会自动做文件名安全处理）
  title:       人类可读标题（可选，默认同 name）
  inherit_from: 要继承的旧会话名称（可选）。设置后会将旧会话的截图和字幕复制到新会话。

返回: {"name": "20260719_1200_第10讲", "path": "...", "inherited_from": "...", "screenshot_count": 24}

典型用法:
  - 看新课: session_create(name="ROS2入门21讲", title="【古月居】ROS2入门21讲")
  - 继承上一讲截图: session_create(name="第11讲", inherit_from="20260719_1135_第10讲")""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "会话名称"},
                "title": {"type": "string", "description": "人类可读标题（可选）"},
                "inherit_from": {"type": "string", "description": "要继承的旧会话名称（可选）"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "session_switch",
        "description": """切换到指定的已有会话（后续 screenshot、caption 都写入该会话）。

参数:
  name: 会话名称（从 session_list 获取）

返回: 会话详情（同 session_status）""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "会话名称"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "session_status",
        "description": """查看当前会话（或指定会话）的详细信息。

返回:
  - name, title, created_at, path
  - meta: 完整元数据
  - screenshots: 截图文件列表
  - screenshot_count: 截图数量
  - caption_file: 字幕文件路径

不带参数返回当前活跃会话；带 name 参数返回指定会话。""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "会话名称（可选，默认当前）"},
            },
            "required": [],
        },
    },

    # ── 浏览器控制 ──
    {
        "name": "browser_navigate",
        "description": """导航到指定 URL。自动等待 SPA 渲染完成。

参数:
  url: 完整网址

返回: {"title": "页面标题", "url": "...", "elements": [...], "has_video": true/false}""",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "完整网址"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_seek",
        "description": """跳转到视频指定秒数。

参数:
  time: 目标秒数（浮点数）""",
        "inputSchema": {
            "type": "object",
            "properties": {"time": {"type": "number", "description": "目标秒数"}},
            "required": ["time"],
        },
    },
    {
        "name": "browser_pause",
        "description": "暂停视频播放。返回当前时间。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_play",
        "description": "继续视频播放。返回当前时间。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_screenshot",
        "description": """对视频/页面截图，保存到当前会话的 screenshots/ 目录。

参数:
  name: 截图描述（用于文件名，比如 "架构图" → 0150s_架构图.png）

返回: {"path": "...", "time": 150.5, "size_kb": 123}""",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "截图描述（简短中文）"}},
            "required": ["name"],
        },
    },
    {
        "name": "browser_get_page",
        "description": """获取当前页面的交互元素列表（AX Tree 优先，DOM 扫描兜底）。

返回:
  - title, url
  - elements: [{type: "link"/"button"/"heading"/"clickable"/..., text: "...", href?: "..."}, ...]
  - source: "ax_tree" | "dom"

DS 拿到后可以分析页面内容、找到目标链接/按钮。""",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_snapshot",
        "description": """【推荐】全面扫描当前页面所有元素，不做语义分类，全量返回给 LLM 自己判断。

与 browser_get_page 的核心区别：
  - 捕获 <iframe>（很多视频/教育平台用 iframe 加载播放器）
  - 捕获 <video>/<audio> 标签及其播放状态
  - 返回页面统计摘要（链接数、按钮数、iframe数等），帮助快速判断页面类型
  - 不做"导航链接""视频相关"等预设分类，元素按原生 HTML 标签返回 (tag: "a"/"button"/"iframe"/"video"/"h1"等)

返回: {title, url, elements: [{tag, text, href?, src?, ...}], stats: {total_links, total_iframes, ...}, source: "scan_page"}

适用场景：不确定页面类型的网站、非标视频站、iframe 内嵌播放器、SPA。""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_elements": {"type": "integer", "description": "最大元素数（默认200）", "default": 200},
            },
            "required": [],
        },
    },
    {
        "name": "browser_click",
        "description": """点击页面上文本为 text 的可见元素（按钮/链接/任意可点击元素）。

参数:
  text:  要点击的元素文本（精确匹配优先，回退模糊匹配）
  index: 匹配到多个时的序号（0=第1个，默认0）

返回: {"clicked": true, "text": "...", "index": 0}""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "元素可见文本"},
                "index": {"type": "integer", "description": "匹配序号（默认0）", "default": 0},
            },
            "required": ["text"],
        },
    },
    {
        "name": "browser_status",
        "description": """获取当前视频/页面状态。

返回:
  - page_title, page_url
  - has_video: 是否有视频
  - current_time, duration, paused, playback_rate""",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_connect",
        "description": """连接或重启 Edge 浏览器（调试模式）。

参数:
  auto_start: 0=仅连接已有 CDP（默认）, 1=自动启动 Edge 调试模式

返回:
  - connected: 是否连接成功
  - page_title: 当前页面标题
  - message: 状态描述""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "auto_start": {"type": "integer", "enum": [0, 1], "default": 0,
                               "description": "0=被动连接, 1=自动启动 Edge"},
            },
            "required": [],
        },
    },

    # ── 转录 ──
    {
        "name": "transcription_start",
        "description": """启动 Whisper 实时转录。字幕写入当前会话文件夹。

参数:
  show_gui: 0=静默后台, 1=显示悬浮字幕窗口""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "show_gui": {"type": "integer", "enum": [0, 1], "default": 0,
                             "description": "0=静默, 1=显示悬浮窗"},
            },
            "required": [],
        },
    },
    {
        "name": "transcription_read",
        "description": """读取最近转录的字幕文本。

参数:
  tail: 读取最近 N 行（默认80）

返回: {"transcript": "...", "line_count": 35}""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tail": {"type": "integer", "default": 80, "description": "读取最近 N 行"},
            },
            "required": [],
        },
    },
    {
        "name": "transcription_stop",
        "description": "停止转录，返回完整字幕。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },

    # ── 分析循环 ──
    {
        "name": "analysis_start",
        "description": """启动自动分析循环。

工作流:
  1. 等待 60s 字幕积累
  2. 每 60s 一轮: 读视频状态 + 新增字幕 → DS 决策 → 执行 seek/pause/screenshot/play
  3. 视频结束后自动停止

前置条件: 已创建会话 + 已启动转录 + 视频正在播放""",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "analysis_stop",
        "description": "停止自动分析循环。返回统计信息。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "analysis_status",
        "description": """查看分析状态。

返回: {"running": true/false, "round": 3, "video_time": 245.0, "screenshots_taken": 8}""",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },

    # ── DS 决策 ──
    {
        "name": "ds_decide",
        "description": """让 DeepSeek 分析字幕文本，决定截图位置和动作。

参数:
  subtitle: 字幕文本（含时间戳）
  video_time: 当前视频秒数（可选）
  duration: 视频总时长（可选）

返回: {"summary": "...", "actions": [{"type": "seek", "time": 120}, {"type": "pause"}, ...]}""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subtitle": {"type": "string", "description": "字幕文本"},
                "video_time": {"type": "number", "description": "当前时间"},
                "duration": {"type": "number", "description": "总时长"},
            },
            "required": ["subtitle"],
        },
    },
]


# ═══════════════════════════════════════════════════════════
#  全局状态
# ═══════════════════════════════════════════════════════════

_current_session: str = ""  # 当前活跃会话名
_screenshot_dir: str = ""   # 当前会话截图目录
_caption_log_path: str = "" # 当前字幕文件

# 浏览器
_browser: "BrowserController | None" = None
_async_loop: "asyncio.AbstractEventLoop | None" = None
_async_thread: "threading.Thread | None" = None

# 转录
_capture = None
_transcriber = None
_caption_running = False
_caption_start_time = 0.0

# 分析
_analyzing = False
_stop_requested = False
_analysis_thread: "threading.Thread | None" = None
_last_caption_line = 0
_analysis_round = 0
_screenshots_taken = 0

# DeepSeek
_ds: "DeepSeekClient | None" = None


# ═══════════════════════════════════════════════════════════
#  asyncio 桥接
# ═══════════════════════════════════════════════════════════

def _ensure_async_loop():
    global _async_loop, _async_thread
    if _async_loop is not None and not _async_loop.is_closed():
        return
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        global _async_loop
        _async_loop = loop
        loop.run_forever()
    _async_thread = threading.Thread(target=_run, daemon=True)
    _async_thread.start()
    while _async_loop is None:
        time.sleep(0.05)


def _run_async(coro, timeout=30):
    if _async_loop is None:
        raise RuntimeError("asyncio loop not started")
    future = asyncio.run_coroutine_threadsafe(coro, _async_loop)
    return future.result(timeout=timeout)


# ═══════════════════════════════════════════════════════════
#  DS 初始化
# ═══════════════════════════════════════════════════════════

def _ensure_ds():
    global _ds
    if _ds is None:
        _ds = DeepSeekClient()


# ═══════════════════════════════════════════════════════════
#  浏览器初始化
# ═══════════════════════════════════════════════════════════

def _ensure_browser(auto_start: bool = False) -> bool:
    global _browser
    if _browser is None:
        if not HAS_BROWSER:
            return False
        _browser = BrowserController()
        _ensure_async_loop()
        ok = _run_async(_browser.connect(auto_start=auto_start), timeout=120 if auto_start else 30)
        if not ok:
            _browser = None
            return False
    return True


# ═══════════════════════════════════════════════════════════
#  会话工具实现
# ═══════════════════════════════════════════════════════════

def _session_list() -> dict:
    return list_sessions()


def _session_create(args: dict) -> dict:
    global _current_session, _screenshot_dir
    name = args.get("name", "")
    title = args.get("title", "")
    inherit = args.get("inherit_from", "")
    result = create_session(name, title, inherit_from=inherit)
    _current_session = result["name"]
    _screenshot_dir = get_screenshot_dir(_current_session)
    return result


def _session_switch(args: dict) -> dict:
    global _current_session, _screenshot_dir
    name = args.get("name", "")
    info = get_session_info(name)
    if info.get("status") == "error":
        return info
    _current_session = name
    _screenshot_dir = get_screenshot_dir(_current_session)
    return info


def _session_status(args: dict) -> dict:
    name = args.get("name", "") or _current_session
    if not name:
        return list_sessions()
    return get_session_info(name)


# ═══════════════════════════════════════════════════════════
#  浏览器工具实现
# ═══════════════════════════════════════════════════════════

def _browser_navigate(args: dict) -> dict:
    if not _ensure_browser():
        return {"error": "浏览器未连接"}
    url = args.get("url", "")
    try:
        title, has_video = _run_async(_browser.navigate(url))
        page = _run_async(_browser.get_page_ax_tree(), timeout=5)
        if not page.get("elements"):
            page = _run_async(_browser.get_page_structure(), timeout=10)
        return {
            "title": title,
            "url": url,
            "has_video": has_video,
            "elements": page.get("elements", []),
        }
    except Exception as e:
        return {"error": str(e)}


def _browser_seek(args: dict) -> dict:
    if not _ensure_browser():
        return {"error": "浏览器未连接"}
    t = float(args.get("time", 0))
    try:
        drift = _run_async(_browser.seek(t))
        return {"seek_to": t, "drift": drift}
    except Exception as e:
        return {"error": str(e)}


def _browser_pause() -> dict:
    if not _ensure_browser():
        return {"error": "浏览器未连接"}
    try:
        ct = _run_async(_browser.pause())
        return {"paused": True, "current_time": ct}
    except Exception as e:
        return {"error": str(e)}


def _browser_play() -> dict:
    if not _ensure_browser():
        return {"error": "浏览器未连接"}
    try:
        ct = _run_async(_browser.play())
        return {"paused": False, "current_time": ct}
    except Exception as e:
        return {"error": str(e)}


def _browser_screenshot(args: dict) -> dict:
    global _screenshots_taken
    if not _ensure_browser():
        return {"error": "浏览器未连接"}
    name = args.get("name", "screenshot")
    # 确保有截图目录
    ss_dir = _screenshot_dir
    if not ss_dir and _current_session:
        ss_dir = get_screenshot_dir(_current_session)
    if not ss_dir:
        ss_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
    os.makedirs(ss_dir, exist_ok=True)

    try:
        t = _run_async(_browser.get_current_time())
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', name).strip().replace(" ", "_")[:30]
        minutes = int(t // 60)
        seconds = int(t % 60)
        fname = f"{minutes:02d}{seconds:02d}s_{safe_name}.png"
        path = os.path.join(ss_dir, fname)
        _run_async(_browser.screenshot(path))
        size_kb = os.path.getsize(path) // 1024 if os.path.exists(path) else 0
        _screenshots_taken += 1
        return {"path": path, "time": t, "size_kb": size_kb}
    except Exception as e:
        return {"error": str(e)}


def _browser_get_page() -> dict:
    if not _ensure_browser():
        return {"error": "浏览器未连接"}
    try:
        # 首选：全量扫描（含 iframe、所有链接、统计摘要），LLM 自己判断
        scan = _run_async(_browser.scan_page(max_elements=200), timeout=10)
        if scan and not scan.get("error") and scan.get("elements"):
            return scan
        # 备用1：AX tree（无障碍树，适用于标准 HTML 页面）
        ax = _run_async(_browser.get_page_ax_tree(), timeout=5)
        if ax.get("elements"):
            return ax
        # 备用2：DOM CSS class 扫描（兜底）
        return _run_async(_browser.get_page_structure(), timeout=10)
    except Exception as e:
        return {"error": str(e)}


def _browser_scan(args: dict) -> dict:
    """独立的页面全量快照工具（不走 fallback 链，直接调用 scan_page）。"""
    if not _ensure_browser():
        return {"error": "浏览器未连接"}
    try:
        max_el = int(args.get("max_elements", 200))
        return _run_async(_browser.scan_page(max_elements=max_el), timeout=10)
    except Exception as e:
        return {"error": str(e)}


def _browser_click(args: dict) -> dict:
    if not _ensure_browser():
        return {"error": "浏览器未连接"}
    text = args.get("text", "")
    index = args.get("index", 0)
    try:
        ok = _run_async(_browser.click_element(text=text, index=index))
        return {"clicked": ok, "text": text, "index": index}
    except Exception as e:
        return {"error": str(e)}


def _browser_status() -> dict:
    if not _ensure_browser():
        return {"error": "浏览器未连接"}
    try:
        state = _run_async(_browser.get_state())
        if state:
            result = dict(state)  # copy
            print(f"[MCP] browser_status → has_video=True, url={result.get('page_url','')[:60]}", file=sys.stderr)
            return result
        # 浏览器已连接但 get_state() 返回 None（页面无 video 元素）
        # 直接用 page 获取标题和 URL，不依赖 ensure_active_tab 是否成功
        try:
            p = _browser._page
            title = ""
            url = ""
            if p:
                title = _run_async(p.title()) or ""
                url = p.url or ""
            result = {
                "page_title": title,
                "page_url": url,
                "has_video": False,
                "current_time": 0,
                "duration": 0,
                "paused": True,
                "playback_rate": 0,
                "video_width": 0,
                "video_height": 0,
            }
            print(f"[MCP] browser_status → has_video=False, url={url[:60]}, title={title[:40]}", file=sys.stderr)
            return result
        except Exception:
            print("[MCP] browser_status → fallback 失败，返回空", file=sys.stderr)
            return {"page_title": "", "page_url": "", "has_video": False}
    except Exception as e:
        return {"error": str(e)}


def _browser_connect(args: dict) -> dict:
    """主动连接浏览器。launch_persistent_context 是默认方式，无需 CDP 端口。"""
    global _browser
    auto_start = bool(args.get("auto_start", 0))

    # 如果 browser 已连接，先检查是否还活着
    if _browser is not None:
        try:
            _run_async(_browser.get_state(), timeout=5)
            return {"connected": True, "message": "浏览器已连接", "page_title": ""}
        except Exception:
            _browser = None  # 连接已失效，重新连接

    if not HAS_BROWSER:
        return {"connected": False, "message": "BrowserController 模块不可用"}

    _browser = BrowserController()
    _ensure_async_loop()

    # launch_persistent_context 是首选，通常 5-15 秒，留 60 秒余量
    ok = _run_async(_browser.connect(auto_start=True), timeout=60)
    if not ok:
        _browser = None
        return {
            "connected": False,
            "message": (
                "Edge 启动失败。可能原因:\n"
                "  - Edge 未安装或版本过旧\n"
                "  - 系统权限不足\n"
                "  - Playwright 未安装 Edge 通道: playwright install chromium"
            ),
        }

    # 获取当前页面信息
    try:
        state = _run_async(_browser.get_state())
        if state:
            return {
                "connected": True,
                "message": "浏览器连接成功",
                "page_title": state.get("page_title", ""),
                "page_url": state.get("page_url", ""),
                "has_video": state.get("has_video", False),
            }
        # 无 video 的普通页面：直接从 page 对象取标题
        try:
            title = _run_async(_browser._page.title()) if _browser._page else ""
            url = _browser._page.url if _browser._page else ""
            return {
                "connected": True,
                "message": "浏览器连接成功",
                "page_title": title,
                "page_url": url,
                "has_video": False,
            }
        except Exception:
            return {"connected": True, "message": "浏览器连接成功"}
    except Exception:
        return {"connected": True, "message": "浏览器连接成功（页面信息获取失败）"}


# ═══════════════════════════════════════════════════════════
#  转录工具实现
# ═══════════════════════════════════════════════════════════

def _transcription_start(args: dict) -> dict:
    global _capture, _transcriber, _caption_log_path, _caption_running, _caption_start_time
    if not _ensure_transcriber():
        return {"status": "error", "message": "转录模块不可用"}
    AudioCapture, Transcriber, check_transcriber_deps_get = _transcriber_module
    if not check_transcriber_deps_get():
        return {"status": "error", "message": "依赖缺失: pip install soundcard faster-whisper"}
    if _caption_running:
        return {"status": "already_running", "log_path": _caption_log_path}

    import queue as qmod

    # 确定保存目录
    save_dir = _screenshot_dir
    if not save_dir and _current_session:
        save_dir = get_screenshot_dir(_current_session)
    if not save_dir:
        save_dir = ROOT

    _caption_log_path = os.path.join(
        save_dir, f"captions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )
    _caption_start_time = time.time()

    audio_q = qmod.Queue()

    def on_text(text):
        pass

    _capture = AudioCapture(audio_q)
    _transcriber = Transcriber(audio_q, on_text, save_path=_caption_log_path)
    _capture.start()
    _transcriber.start()
    _caption_running = True

    print(f"[MCP] 转录已启动 → {os.path.basename(_caption_log_path)}", file=sys.stderr)
    return {
        "status": "started",
        "log_path": _caption_log_path,
        "show_gui": bool(args.get("show_gui", 0)),
    }


def _transcription_read(args: dict) -> dict:
    tail = args.get("tail", 80)
    if not _caption_log_path or not os.path.exists(_caption_log_path):
        return {"transcript": "", "line_count": 0}

    with open(_caption_log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    content = [
        l.rstrip() for l in lines
        if not l.startswith("===") and l.strip() != "---" and l.strip()
    ]
    recent = content[-tail:] if len(content) > tail else content
    return {
        "transcript": "\n".join(recent),
        "line_count": len(recent),
        "log_path": _caption_log_path,
        "running": _caption_running,
    }


def _transcription_stop() -> dict:
    global _capture, _transcriber, _caption_running
    if _capture:
        _capture.stop()
        _capture = None
    if _transcriber:
        _transcriber.stop()
        _transcriber = None
    _caption_running = False

    # 返回完整字幕
    result = _transcription_read({"tail": 99999})
    result["status"] = "stopped"
    return result


# ═══════════════════════════════════════════════════════════
#  DS 决策工具
# ═══════════════════════════════════════════════════════════

def _ds_decide(args: dict) -> dict:
    _ensure_ds()
    subtitle = args.get("subtitle", "")
    video_time = args.get("video_time", 0)
    duration = args.get("duration", 0)

    video_state = None
    if video_time or duration:
        video_state = {"video_time": video_time, "duration": duration, "paused": False}

    try:
        result = _ds.analyze_screenshots(subtitle, video_state)
        return result
    except Exception as e:
        return {"summary": "", "actions": [], "error": str(e)}


# ═══════════════════════════════════════════════════════════
#  分析循环
# ═══════════════════════════════════════════════════════════

def _analysis_start() -> dict:
    global _analyzing, _stop_requested, _analysis_thread, _last_caption_line, _analysis_round, _screenshots_taken
    if _analyzing:
        return {"status": "already_running"}

    if not HAS_BROWSER or _browser is None:
        return {"status": "error", "message": "浏览器未连接"}
    if not _caption_running:
        return {"status": "error", "message": "字幕未启动，请先 transcription_start"}

    _stop_requested = False
    _analyzing = True
    _last_caption_line = 0
    _analysis_round = 0
    _screenshots_taken = 0
    _ensure_ds()

    _analysis_thread = threading.Thread(target=_analysis_loop, daemon=True)
    _analysis_thread.start()
    return {"status": "started", "message": "分析已启动，等待60s字幕积累..."}


def _analysis_loop():
    global _analyzing, _stop_requested, _analysis_round, _screenshots_taken, _last_caption_line

    # 先等字幕积累
    print(f"[MCP] 分析: 等待字幕积累 60 秒...", file=sys.stderr)
    for i in range(60):
        if _stop_requested:
            _analyzing = False
            print(f"[MCP] 分析: 已停止", file=sys.stderr)
            return
        time.sleep(1)

    print(f"[MCP] 分析: 字幕积累完成，开始循环", file=sys.stderr)
    while not _stop_requested and _analyzing:
        _analysis_round += 1
        print(f"[MCP] 分析: 第 {_analysis_round} 轮 — 读取字幕...", file=sys.stderr)

        # 1. 读视频状态
        video_state = {"video_time": 0, "duration": 0, "paused": False}
        try:
            state = _run_async(_browser.get_state())
            if state:
                video_state = {
                    "video_time": state.get("current_time", 0),
                    "duration": state.get("duration", 0),
                    "paused": state.get("paused", False),
                }
                print(f"[MCP] 分析: 视频状态 — {video_state['video_time']:.0f}s / {video_state['duration']:.0f}s, {'暂停' if video_state['paused'] else '播放中'}", file=sys.stderr)
        except Exception as e:
            print(f"[MCP] 分析: 获取视频状态失败: {e}", file=sys.stderr)
            pass

        # 2. 读字幕（只读新增）
        subtitle = _read_new_captions(max_lines=80)
        if not subtitle.strip():
            print(f"[MCP] 分析: 第 {_analysis_round} 轮 — 无新增字幕，等 10 秒", file=sys.stderr)
            time.sleep(10)
            continue

        caption_lines = subtitle.count('\n') + 1
        print(f"[MCP] 分析: 第 {_analysis_round} 轮 — 新增 {caption_lines} 行字幕，DS 决策中...", file=sys.stderr)

        # 3. DS 决策
        try:
            result = _ds.analyze_screenshots(subtitle, video_state)
            actions = result.get("actions", [])
            summary = result.get("summary", "")
            print(f"[MCP] 分析: DS 摘要 — {summary}", file=sys.stderr)
            print(f"[MCP] 分析: DS 动作 — {len(actions)} 个", file=sys.stderr)
        except Exception as e:
            print(f"[MCP] 分析: DS 决策失败: {e}", file=sys.stderr)
            time.sleep(10)
            continue

        if not actions:
            print(f"[MCP] 分析: 第 {_analysis_round} 轮 — 无截图动作，等 60 秒", file=sys.stderr)
            time.sleep(60)
            continue

        # 4. 执行动作
        for act in actions:
            if _stop_requested:
                break
            t = act.get("type", "")
            try:
                if t == "seek":
                    target = float(act.get("time", 0))
                    print(f"[MCP] 分析: seek → {target:.0f}s", file=sys.stderr)
                    _run_async(_browser.seek(target))
                elif t == "pause":
                    print(f"[MCP] 分析: pause", file=sys.stderr)
                    _run_async(_browser.pause())
                elif t == "play":
                    print(f"[MCP] 分析: play", file=sys.stderr)
                    _run_async(_browser.play())
                elif t == "screenshot":
                    name = act.get("name", "capture")
                    print(f"[MCP] 分析: screenshot → {name}", file=sys.stderr)
                    _browser_screenshot({"name": name})
                    _screenshots_taken += 1
                time.sleep(0.5)
            except Exception as e:
                print(f"[MCP] 分析: 动作 {t} 失败: {e}", file=sys.stderr)

        print(f"[MCP] 分析: 第 {_analysis_round} 轮 — 完成，已截图 {_screenshots_taken} 张，等 60 秒", file=sys.stderr)
        time.sleep(60)

    _analyzing = False
    print(f"[MCP] 分析: 已停止（共 {_analysis_round} 轮，{_screenshots_taken} 张截图）", file=sys.stderr)


def _read_new_captions(max_lines: int = 80) -> str:
    global _last_caption_line
    if not _caption_log_path or not os.path.exists(_caption_log_path):
        return ""

    with open(_caption_log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    content = [
        l.rstrip() for l in lines
        if not l.startswith("===") and l.strip() != "---" and l.strip()
    ]

    if _last_caption_line >= len(content):
        return ""
    new_lines = content[_last_caption_line:]
    _last_caption_line = len(content)
    if len(new_lines) > max_lines:
        new_lines = new_lines[-max_lines:]
    return "\n".join(new_lines)


def _analysis_stop() -> dict:
    global _analyzing, _stop_requested
    _stop_requested = True
    _analyzing = False
    return {
        "status": "stopped",
        "rounds": _analysis_round,
        "screenshots_taken": _screenshots_taken,
    }


def _analysis_status() -> dict:
    return {
        "running": _analyzing,
        "round": _analysis_round,
        "screenshots_taken": _screenshots_taken,
        "caption_running": _caption_running,
    }


# ═══════════════════════════════════════════════════════════
#  JSON-RPC 路由
# ═══════════════════════════════════════════════════════════

def handle_request(req: dict) -> dict | None:
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params", {}) or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "videoagent-mcp", "version": "1.0"},
            }
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        return _call_tool(mid, params)

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def _call_tool(mid, params: dict) -> dict:
    """路由工具调用。"""
    tool_name = params.get("name", "")
    args = params.get("arguments", {}) or {}

    # 路由表: tool_name → handler (接受 args dict，返回 result dict)
    handlers = {
        # 会话
        "session_list":       lambda a: _session_list(),
        "session_create":     lambda a: _session_create(a),
        "session_switch":     lambda a: _session_switch(a),
        "session_status":     lambda a: _session_status(a),
        # 浏览器
        "browser_navigate":   lambda a: _browser_navigate(a),
        "browser_seek":       lambda a: _browser_seek(a),
        "browser_pause":      lambda a: _browser_pause(),
        "browser_play":       lambda a: _browser_play(),
        "browser_screenshot": lambda a: _browser_screenshot(a),
        "browser_get_page":   lambda a: _browser_get_page(),
        "browser_snapshot":   lambda a: _browser_scan(a),
        "browser_click":      lambda a: _browser_click(a),
        "browser_status":     lambda a: _browser_status(),
        "browser_connect":    lambda a: _browser_connect(a),
        # 转录
        "transcription_start": lambda a: _transcription_start(a),
        "transcription_read":  lambda a: _transcription_read(a),
        "transcription_stop":  lambda a: _transcription_stop(),
        # 分析
        "analysis_start":     lambda a: _analysis_start(),
        "analysis_stop":      lambda a: _analysis_stop(),
        "analysis_status":    lambda a: _analysis_status(),
        # DS
        "ds_decide":          lambda a: _ds_decide(a),
    }

    handler = handlers.get(tool_name)
    if not handler:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}

    try:
        result = handler(args)
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        }
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {"content": [{"type": "text", "text": json.dumps({"error": str(e)}, ensure_ascii=False)}]}
        }


# ═══════════════════════════════════════════════════════════
#  main — stdio JSON-RPC 主循环
# ═══════════════════════════════════════════════════════════

def main():
    mode = "--port" in sys.argv

    if mode:
        # HTTP 模式（调试用）
        port_idx = sys.argv.index("--port") + 1
        port = int(sys.argv[port_idx]) if port_idx < len(sys.argv) else 8100
        _run_http(port)
    else:
        # stdio 模式 — 强制 UTF-8 输出，避免 Windows GBK 终端编码报错
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print("[VideoAgent MCP] 启动 (stdio)", file=sys.stderr)
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
                print(f"[VideoAgent MCP] 错误: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc(file=sys.stderr)


def _run_http(port: int):
    """简易 HTTP JSON-RPC 模式（调试用）。"""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                req = json.loads(body)
                resp = handle_request(req)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if resp is not None:
                    self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())
            except Exception:
                self.send_response(400)
                self.end_headers()

    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"[VideoAgent MCP] HTTP 模式: http://127.0.0.1:{port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
