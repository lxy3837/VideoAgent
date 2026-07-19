"""
浏览器控制器 — 通过 CDP (Chrome DevTools Protocol) 连接用户桌面上的 Edge，
获取精确 video.currentTime、截图、操控播放。

前提: Edge 以远程调试模式启动:
  msedge.exe --remote-debugging-port=9222

连接方式:
  playwright.chromium.connect_over_cdp("http://localhost:9222")
  连接后获得用户浏览器上所有标签页，登录状态/Cookie 全部保留。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import subprocess
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import TypedDict

try:
    from playwright.async_api import async_playwright, Browser, Page
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


CDP_DEFAULT_PORT = 9222
CDP_SCAN_RANGE = (9222, 9232)  # 扫描端口范围
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


# ── 类型定义 ────────────────────────────────────────────

class VideoState(TypedDict):
    page_title: str
    page_url: str
    has_video: bool
    current_time: float       # 秒
    duration: float           # 秒
    paused: bool
    playback_rate: float
    video_width: int
    video_height: int


class CaptureResult(TypedDict):
    time: float
    name: str
    path: str
    width: int
    height: int


# ── CDP 浏览器管理 ──────────────────────────────────────

def _find_edge() -> str | None:
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    import winreg
    for key_path in [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
    ]:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                path, _ = winreg.QueryValueEx(key, "")
                if os.path.exists(path):
                    return path
        except OSError:
            continue
    return None


def _tcp_ping(host: str, port: int, timeout: float = 1.0) -> bool:
    """原生 socket 连接，检测端口是否在监听。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def is_cdp_running(port: int = CDP_DEFAULT_PORT) -> bool:
    """
    检查 CDP 是否就绪。
    先 TCP 探活，再 HTTP 验证（绕过 urllib/代理/firewall 干扰）。
    """
    # 第一步：原生 TCP socket ping
    tcp_ok = _tcp_ping("127.0.0.1", port) or _tcp_ping("localhost", port)
    if not tcp_ok:
        return False

    # 第二步：HTTP 请求确认 CDP 服务可用
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    for host in ["127.0.0.1", "localhost"]:
        url = f"http://{host}:{port}/json/version"
        try:
            req = opener.open(url, timeout=2)
            data = req.read()
            req.close()
            if b"Browser" in data:
                return True
        except Exception:
            continue

    # 第三步：回退 /json/list
    for host in ["127.0.0.1", "localhost"]:
        url = f"http://{host}:{port}/json/list"
        try:
            req = opener.open(url, timeout=2)
            data = req.read()
            req.close()
            if data.strip().startswith(b"["):
                return True
        except Exception:
            continue

    return False


def _kill_and_launch_edge_cdp(port: int = CDP_DEFAULT_PORT, timeout: float = 30.0) -> bool:
    """杀 Edge → 复制真实 Profile → 用独立 user-data-dir 启动带 CDP 的 Edge。
    
    关键：
    - 显式 --user-data-dir 确保 Edge 创建独立实例，不跟已有实例合并
    - 复制真实 Profile 保留所有登录态/Cookie
    - 全程同步执行，不给 Edge 自动复活的机会
    
    返回 True 表示 CDP 端口已就绪。
    """
    edge_path = _find_edge()
    if not edge_path:
        print("[Browser] 找不到 Edge 安装路径")
        return False

    real_profile = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
    agent_profile = str(Path(os.environ.get("TEMP", ".")) / "video_agent_profile")

    # 1. 优雅关闭（保存会话）
    _kill_all_edge(False)
    time.sleep(2)

    # 2. 强制清理残留
    _kill_all_edge(True)
    time.sleep(0.5)

    # 3. 复制 Profile（仅关键登录数据，非全量拷贝）
    _copy_profile_fast(real_profile, agent_profile)

    # 4. 启动独立 Edge 实例（独立 user-data-dir → CDP 必定生效）
    return _start_edge_cdp(edge_path, port, user_data_dir=agent_profile, timeout=timeout)


def _copy_profile_fast(src: str, dst: str):
    """快速复制 Edge Profile 的关键登录数据。
    
    不复制 Cache、GPUCache、Code Cache 等大目录。
    """
    # 登录相关的关键文件/目录
    key_items = [
        # 登录核心
        "Cookies", "Cookies-journal",
        "Login Data", "Login Data-journal",
        "Web Data", "Web Data-journal",
        # 偏好设置
        "Preferences",
        # 存储
        "Local Storage",
        "Session Storage",
        # 扩展和同步
        "Extensions",
        "Sync Data",
        "Bookmarks",
        # 网络状态
        "Network",
        "TransportSecurity",
    ]

    try:
        os.makedirs(dst, exist_ok=True)
        for item in key_items:
            src_path = os.path.join(src, item)
            dst_path = os.path.join(dst, item)
            if os.path.isdir(src_path):
                if os.path.exists(dst_path):
                    shutil.rmtree(dst_path, ignore_errors=True)
                shutil.copytree(src_path, dst_path)
            elif os.path.isfile(src_path):
                shutil.copy2(src_path, dst_path)

        # 复制 Default 目录下的关键文件（Profile 1 默认在这里）
        default_src = os.path.join(src, "Default")
        default_dst = os.path.join(dst, "Default")
        if os.path.isdir(default_src):
            os.makedirs(default_dst, exist_ok=True)
            for item in key_items:
                src_path = os.path.join(default_src, item)
                dst_path = os.path.join(default_dst, item)
                if os.path.isdir(src_path):
                    if os.path.exists(dst_path):
                        shutil.rmtree(dst_path, ignore_errors=True)
                    shutil.copytree(src_path, dst_path)
                elif os.path.isfile(src_path):
                    shutil.copy2(src_path, dst_path)

        # 写入 First Run 标记（跳过首次引导）
        with open(os.path.join(dst, "First Run"), "w") as f:
            f.write("")

        # 写入 Local State（Edge 需要）
        local_state_src = os.path.join(src, "Local State")
        if os.path.isfile(local_state_src):
            shutil.copy2(local_state_src, os.path.join(dst, "Local State"))

        print(f"[Browser] Profile 已复制 ({sum(1 for _ in Path(dst).rglob('*'))} 文件)")
    except Exception as e:
        print(f"[Browser] Profile 复制异常（可能部分登录态丢失）: {e}")


def _is_edge_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/fi", "IMAGENAME eq msedge.exe", "/fo", "csv", "/nh"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "msedge.exe" in result.stdout
    except Exception:
        return False


def _kill_all_edge(force: bool = True):
    """杀掉所有 Edge 相关进程。
    force=False: 优雅关闭（保存会话），等 3s 后再强制清理残留
    force=True:  直接强制杀（不保存会话）
    """
    names = ["msedge.exe", "msedgewebview2.exe",
             "MicrosoftEdgeUpdate.exe", "MicrosoftEdgeUpdateCore.exe"]

    if not force:
        # 先优雅关闭主进程（不加 /f），让 Edge 有机会保存会话
        try:
            subprocess.run(
                ["taskkill", "/t", "/im", "msedge.exe"],
                capture_output=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
        time.sleep(3)  # 等 Edge 写完会话数据

    # 强制清理残留
    for name in names:
        try:
            subprocess.run(
                ["taskkill", "/f", "/t", "/im", name],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass

    try:
        subprocess.run(
            ["powershell", "-Command",
             "Get-Process msedge* -ErrorAction SilentlyContinue | Stop-Process -Force"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass



import shutil


def _start_edge_cdp(edge_path: str, port: int, user_data_dir: str = "", timeout: float = 30.0) -> bool:
    """直接启动 Edge 可执行文件 + CDP 端口 + 独立 user-data-dir。
    
    user_data_dir: 显式指定用户数据目录。如果不指定则用默认目录。
    关键：必须用独立目录，否则 Edge 会连到已有实例，CDP 参数被忽略。
    """
    args = [
        edge_path,
        f"--remote-debugging-port={port}",
        "--restore-last-session",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-features=msEdgeAutoRestartOnCrash,msEdgeSidebar",
    ]
    if user_data_dir:
        args.insert(1, f"--user-data-dir={user_data_dir}")

    print(f"[Browser] 正在启动 Edge (端口 {port})...")
    print(f"[Browser] 命令: {edge_path} --remote-debugging-port={port}")
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        print(f"[Browser] Edge 进程已启动 (PID={proc.pid})")
    except Exception as e:
        print(f"[Browser] Edge 启动失败: {e}")
        return False

    # 等待 CDP 就绪
    deadline = time.time() + timeout
    last_log = 0
    while time.time() < deadline:
        if is_cdp_running(port):
            print(f"[Browser] CDP localhost:{port} 就绪 ✓")
            return True
        now = time.time()
        if now - last_log >= 2.0:
            # 确认进程还在
            alive = proc.poll() is None if proc else "?"
            print(f"[Browser] 等待 CDP... (剩余 {deadline - now:.0f}s, Edge:{'存活' if alive else '已退出'})")
            last_log = now
        time.sleep(0.5)

    return False


def find_cdp_port(timeout: float = 30.0) -> int:
    """
    扫描端口范围找到 Edge 实际监听的 CDP 端口。
    返回端口号，找不到返回 0。
    """
    if is_cdp_running(CDP_DEFAULT_PORT):
        return CDP_DEFAULT_PORT

    print(f"[Browser] 9222 未响应，扫描端口...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in range(CDP_SCAN_RANGE[0], CDP_SCAN_RANGE[1] + 1):
            if is_cdp_running(port):
                print(f"[Browser] 找到 CDP 端口: {port} ✓")
                return port
        time.sleep(1)
    return 0


def ensure_edge_cdp(timeout: float = 60.0) -> tuple[int, bool]:
    """
    确保 Edge CDP 可用。
    返回 (port, success)。
    
    策略：
    1. 如果已有 CDP 在监听 → 直接返回端口号
    2. 没有 → 扫描端口范围
    3. 还没有 → 杀 Edge → 启动 Edge → 等待 → 扫描
    """
    # 策略 1：已有 CDP
    port = find_cdp_port(timeout=5)
    if port:
        return port, True

    # 策略 2：杀 Edge + 重启
    edge_path = _find_edge()
    if not edge_path:
        print("[Browser] 找不到 Edge 安装")
        return 0, False

    print("[Browser] 未检测到 CDP，正在重启 Edge 调试模式...")
    _kill_all_edge()

    if _start_edge_cdp(edge_path, CDP_DEFAULT_PORT, timeout=30):
        return CDP_DEFAULT_PORT, True

    # 策略 3：扫描（Edge 可能用了其他端口）
    print("[Browser] 9222 超时，尝试扫描其他端口...")
    port = find_cdp_port(timeout=15)
    if port:
        return port, True

    return 0, False


# ── 页面查找 ────────────────────────────────────────────

async def _find_video_page(browser: Browser) -> Page | None:
    """在所有页面中查找包含媒体内容的页面。

    策略：
    1. 优先检查当前活跃（可见）的页签
    2. 跳过已挂起（discarded）的页签 — 它们 DOM 可能过期，误报 video
    3. 最后遍历其他活跃页签
    """
    pages = browser.contexts[0].pages if browser.contexts else []
    if not pages:
        return None

    # 先按活跃度排序：可见的排前面，挂起的排后面
    active_pages = []
    suspended_pages = []
    for page in pages:
        try:
            is_visible = await page.evaluate("() => document.visibilityState === 'visible'")
            if is_visible:
                active_pages.insert(0, page)  # 当前可见的排最前面
            else:
                active_pages.append(page)
        except Exception:
            suspended_pages.append(page)

    ordered = active_pages + suspended_pages

    for page in ordered:
        try:
            # 只检查可见加载完成的页面；挂起的页面 evaluate 会报错自动跳过
            has_media = await page.evaluate("""
                () => {
                    if (document.visibilityState !== 'visible') return false;
                    return !!(
                        document.querySelector('video') ||
                        document.querySelector('audio') ||
                        document.querySelector('iframe[src*="player"]') ||
                        document.querySelector('iframe[src*="video"]') ||
                        document.querySelector('canvas') ||
                        document.querySelector('[data-player]') ||
                        document.querySelector('.bpx-player-video-area')
                    );
                }
            """)
            if has_media:
                return page
        except Exception:
            continue
    return None


async def _get_all_pages(browser: Browser) -> list[dict]:
    """列出浏览器中所有页面，供 GUI 选择。标注可见页、挂起页。"""
    result = []
    if not browser.contexts:
        return result
    for ctx in browser.contexts:
        for page in ctx.pages:
            try:
                title = await page.title()
                url = page.url
                info = await page.evaluate("""
                    () => ({
                        visible: document.visibilityState === 'visible',
                        hasVideo: !!document.querySelector('video'),
                        hasAudio: !!document.querySelector('audio'),
                        hasPlayer: !!(
                            document.querySelector('iframe[src*="player"]') ||
                            document.querySelector('iframe[src*="video"]') ||
                            document.querySelector('canvas') ||
                            document.querySelector('[data-player]') ||
                            document.querySelector('.bpx-player-video-area')
                        ),
                    })
                """)
                result.append({
                    "title": title,
                    "url": url,
                    "has_video": info.get("hasVideo", False) or info.get("hasPlayer", False) or info.get("hasAudio", False),
                    "visible": info.get("visible", True),
                })
            except Exception:
                result.append({
                    "title": "(已挂起/无法访问)",
                    "url": page.url,
                    "has_video": False,
                    "visible": False,
                })
    return result


# ── 视频状态 ────────────────────────────────────────────

async def _read_video_state(page: Page, selector: str = "video") -> VideoState | None:
    """从页面读取视频播放状态（精确到毫秒）。支持自定义 selector。"""
    try:
        state = await page.evaluate(f"""
            () => {{
                const v = document.querySelector('{selector}');
                if (!v) return null;
                return {{
                    current_time: v.currentTime || 0,
                    duration: v.duration || 0,
                    paused: v.paused,
                    playback_rate: v.playbackRate || 1,
                    video_width: v.videoWidth || 0,
                    video_height: v.videoHeight || 0,
                }};
            }}
        """)
        if state is None:
            return None
        return VideoState(
            page_title=await page.title(),
            page_url=page.url,
            has_video=True,
            current_time=float(state["current_time"]),
            duration=float(state["duration"]),
            paused=bool(state["paused"]),
            playback_rate=float(state["playback_rate"]),
            video_width=int(state["video_width"]),
            video_height=int(state["video_height"]),
        )
    except Exception as e:
        print(f"[Browser] 读取视频状态失败: {e}")
        return None


# ── 视频操控 ────────────────────────────────────────────

async def _execute_video_action(page: Page, js: str) -> dict:
    """在视频页执行 JS 并返回结果。"""
    try:
        result = await page.evaluate(js)
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _seek_video(page: Page, target_time: float, selector: str = "video") -> dict:
    """精确跳转到指定时间（秒）。"""
    return await _execute_video_action(page, f"""
        (() => {{
            const v = document.querySelector('{selector}');
            if (!v) return {{error: 'no video'}};
            v.currentTime = {target_time};
            return {{currentTime: v.currentTime, paused: v.paused}};
        }})()
    """)


async def _toggle_play(page: Page, play: bool, selector: str = "video") -> dict:
    """播放或暂停。"""
    action = "v.play()" if play else "v.pause()"
    return await _execute_video_action(page, f"""
        (() => {{
            const v = document.querySelector('{selector}');
            if (!v) return {{error: 'no video'}};
            {action};
            return {{currentTime: v.currentTime, paused: v.paused}};
        }})()
    """)


# ── 截图 ────────────────────────────────────────────────

async def _screenshot_video(
    page: Page,
    save_dir: str,
    video_time: float,
    name: str,
    selector: str = "video",
) -> CaptureResult:
    """截取视频元素当前帧，保存为带语义名称的文件。
    
    优先截取视频元素本身（不含页面其他内容），
    如果视频元素不可用则回退到整页截图。
    
    文件名格式: {MMSS}s_{name}.png
    """
    os.makedirs(save_dir, exist_ok=True)

    minutes = int(video_time // 60)
    seconds = int(video_time % 60)
    prefix = f"{minutes:02d}{seconds:02d}s"

    safe_name = re.sub(r'[\\/:*?"<>|]', '_', name)
    safe_name = safe_name.strip().replace(" ", "_")
    if safe_name:
        filename = f"{prefix}_{safe_name}.png"
    else:
        filename = f"{prefix}.png"

    filepath = os.path.join(save_dir, filename)

    # 优先截取视频元素本身
    actual_w, actual_h = 0, 0
    try:
        element = await page.query_selector(selector)
        if element:
            box = await element.bounding_box()
            if box and box["width"] > 10 and box["height"] > 10:
                await element.screenshot(path=filepath)
                actual_w, actual_h = int(box["width"]), int(box["height"])
            else:
                # 元素不可见，回退整页截图
                await page.screenshot(path=filepath, full_page=False)
        else:
            # 找不到元素，回退整页截图
            await page.screenshot(path=filepath, full_page=False)
    except Exception:
        await page.screenshot(path=filepath, full_page=False)

    return CaptureResult(
        time=video_time,
        name=name,
        path=filepath,
        width=actual_w,
        height=actual_h,
    )


# ═══════════════════════════════════════════════════════════
#  BrowserController — 统一对外接口
# ═══════════════════════════════════════════════════════════

class BrowserController:
    """
    浏览器总控。

    用法:
        ctrl = BrowserController()
        await ctrl.connect()                    # 连接或启动 Edge CDP
        pages = await ctrl.list_pages()          # 列出所有标签页
        await ctrl.select_video_page()           # 自动定位到视频页
        state = await ctrl.get_state()           # 读视频状态
        await ctrl.play()
        await ctrl.pause()
        await ctrl.seek(120.5)
        results = await ctrl.capture_batch(      # 批量截图
            [{"time": 120.5, "name": "架构图"}],
            save_dir="screenshots"
        )
    """

    def __init__(self, port: int = CDP_DEFAULT_PORT):
        self._port = port
        self._playwright = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._proc: subprocess.Popen | None = None
        self._connected = False
        self._video_selector = "video"  # 可自定义的视频元素 CSS 选择器

    # ── 连接 / 断开 ──

    async def connect(self, auto_start: bool = True) -> bool:
        """
        连接 Edge CDP。

        策略：
          1. 9222 端口已监听 → 直接 connect_over_cdp（秒连，不动浏览器）
          2. 未监听 → 提示用户用桌面「Edge (调试模式)」快捷方式启动 Edge

        返回是否连接成功。
        """
        t0 = time.time()
        if not HAS_PLAYWRIGHT:
            print("[Browser] 缺少 playwright，请 pip install playwright && playwright install chromium")
            return False

        print(f"[Browser] [+{time.time()-t0:.1f}s] 启动 Playwright...")
        self._playwright = await async_playwright().start()

        # ── CDP 已就绪 → 直接连 ──
        print(f"[Browser] [+{time.time()-t0:.1f}s] 检查 CDP (localhost:9222)...")
        cdp_ok = await asyncio.to_thread(is_cdp_running)
        if cdp_ok:
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{CDP_DEFAULT_PORT}"
                )
                self._connected = True
                self._port = CDP_DEFAULT_PORT
                print(f"[Browser] [+{time.time()-t0:.1f}s] CDP 连接成功 ✓（不动浏览器，登录态完整）")
                return True
            except Exception as e:
                print(f"[Browser] CDP 连接异常: {e}")

        # ── CDP 未就绪 → 原子化杀+启，用真实 Profile ──
        print(f"[Browser] [+{time.time()-t0:.1f}s] CDP 未就绪，自动重启 Edge...")
        if not auto_start:
            await self._playwright.stop()
            self._playwright = None
            return False

        # 关键：杀+启全程在一个同步调用里（asyncio.to_thread），
        # 中间不 yield，不给 Edge 自动复活的机会
        launched = await asyncio.to_thread(_kill_and_launch_edge_cdp, CDP_DEFAULT_PORT, 30)
        if launched:
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{CDP_DEFAULT_PORT}"
                )
                self._connected = True
                self._port = CDP_DEFAULT_PORT
                print(f"[Browser] [+{time.time()-t0:.1f}s] Edge CDP 连接成功 ✓（独立实例 + 登录态）")
                print(f"[Browser] Profile: %TEMP%\\video_agent_profile")
                return True
            except Exception as e:
                print(f"[Browser] CDP 连接异常: {e}")
        else:
            print(f"[Browser] Edge 重启超时")
            # 兜底：Playwright 临时 Profile
            print(f"[Browser] [+{time.time()-t0:.1f}s] 兜底：Playwright 独立 Edge...")
            tmp_dir = str(Path(os.environ.get("TEMP", ".")) / "video_agent_edge")
            try:
                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=tmp_dir,
                    channel="msedge",
                    headless=False,
                    args=["--no-first-run", "--no-default-browser-check"],
                )
                self._browser = context.browser
                self._connected = True
                print(f"[Browser] [+{time.time()-t0:.1f}s] 独立 Edge 启动成功 ✓（需手动登录）")
                return True
            except Exception as e2:
                print(f"[Browser] 兜底也失败了: {e2}")

        await self._playwright.stop()
        self._playwright = None
        return False

    async def disconnect(self):
        """断开 CDP 连接，不关浏览器。"""
        self._page = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._connected = False

    async def shutdown(self):
        """断开连接并关闭 Edge 进程（如果是我们启动的）。"""
        await self.disconnect()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def has_video_page(self) -> bool:
        return self._page is not None

    async def ensure_active_tab(self) -> bool:
        """确保 self._page 始终指向用户当前正在看的可见标签页。

        如果当前 page 已挂起/不可见/被关闭，自动切换到可见标签页。
        在 get_page_structure、get_state 等之前调用，保证拿到的是用户看到的页面。
        """
        if not self._browser:
            return False

        _NTP_PREFIXES = ("chrome://", "edge://", "about:", "https://ntp.msn.cn")

        def _is_ntp(page_url: str) -> bool:
            """检测是否为空白页/NTP（没有用户内容的页面）。"""
            url = (page_url or "").lower()
            return any(url.startswith(p) for p in _NTP_PREFIXES) or url.strip() == ""

        # 检查当前 page 是否还活着且可见
        if self._page:
            try:
                visible = await self._page.evaluate("() => document.visibilityState === 'visible'")
                if visible and not _is_ntp(self._page.url):
                    return True  # 当前就是可见的，无需切换
            except Exception:
                pass  # page 可能已关闭

        # 当前 page 不可见或已死 → 切到用户正在看的标签页
        # 收集所有可见页，排除 NTP 空白页
        candidate_pages = []
        for ctx in (self._browser.contexts or []):
            for page in ctx.pages:
                try:
                    visible = await page.evaluate("() => document.visibilityState === 'visible'")
                    url = page.url
                    if visible and not _is_ntp(url):
                        # 有实际内容的可见页 → 高优先级
                        candidate_pages.insert(0, page)
                        break  # 一般只有一个可见标签页有实际内容
                    elif visible:
                        # NTP/空白可见页 → 备用
                        candidate_pages.append(page)
                except Exception:
                    continue

        # 优先取有内容的
        for page in candidate_pages:
            self._page = page
            print(f"[Browser] 已切换到可见标签页: {await page.title()}")
            return True

        # 所有标签页都不活跃 → 保留第一个可用的
        for ctx in (self._browser.contexts or []):
            for page in ctx.pages:
                try:
                    await page.title()  # 测试是否可用
                    self._page = page
                    return True
                except Exception:
                    continue

        return False

    # ── 页面管理 ──

    async def list_pages(self) -> list[dict]:
        """列出浏览器中所有标签页。"""
        if not self._browser:
            return []
        return await _get_all_pages(self._browser)

    async def select_video_page(self, page_url: str | None = None) -> bool:
        """定位到包含 <video> 标签的页面。"""
        if not self._browser:
            return False

        if page_url:
            for ctx in self._browser.contexts:
                for page in ctx.pages:
                    if page.url.startswith(page_url):
                        self._page = page
                        return True

        self._page = await _find_video_page(self._browser)
        if self._page:
            print(f"[Browser] 定位到视频页: {await self._page.title()}")
            return True

        print("[Browser] 未找到含视频的标签页")
        return False

    async def scan_page_for_media(self) -> dict:
        """
        扫描当前页面，返回所有可能的媒体播放器信息。
        供 LLM 分析后决定使用哪个元素作为视频源。

        返回:
          {
            "url": "https://...",
            "videos": [{tag, id, class, src, width, height, ...}],
            "iframes": [{src, width, height, ...}],
            "canvases": [{width, height, ...}],
            "custom_players": [{selector, className, ...}],
            "suggestion": "video"  # 默认建议的 selector
          }
        """
        if not self._page:
            return {"error": "no page selected"}

        try:
            info = await self._page.evaluate(r"""
                () => {
                    const result = {
                        url: location.href,
                        title: document.title,
                        videos: [],
                        iframes: [],
                        canvases: [],
                        custom_players: [],
                    };

                    // <video> 标签
                    document.querySelectorAll('video').forEach((v, i) => {
                        result.videos.push({
                            index: i,
                            tag: 'video',
                            id: v.id || '',
                            className: v.className || '',
                            src: (v.src || v.querySelector('source')?.src || ''),
                            width: v.videoWidth || v.clientWidth || 0,
                            height: v.videoHeight || v.clientHeight || 0,
                            duration: v.duration || 0,
                            paused: v.paused,
                            currentTime: v.currentTime || 0,
                            readyState: v.readyState,
                        });
                    });

                    // <audio> 标签
                    document.querySelectorAll('audio').forEach((a, i) => {
                        result.videos.push({
                            index: i,
                            tag: 'audio',
                            id: a.id || '',
                            className: a.className || '',
                            src: (a.src || a.querySelector('source')?.src || ''),
                            duration: a.duration || 0,
                            paused: a.paused,
                        });
                    });

                    // <iframe> 元素（可能是嵌入式播放器）
                    document.querySelectorAll('iframe').forEach((f, i) => {
                        const rect = f.getBoundingClientRect();
                        if (rect.width > 200 || rect.height > 100) {  // 过滤小 iframe
                            result.iframes.push({
                                index: i,
                                src: f.src || '',
                                id: f.id || '',
                                className: f.className || '',
                                width: rect.width,
                                height: rect.height,
                                visible: rect.width > 0 && rect.height > 0,
                            });
                        }
                    });

                    // <canvas> 元素（可能是 WebGL/Canvas 渲染的视频）
                    document.querySelectorAll('canvas').forEach((c, i) => {
                        const rect = c.getBoundingClientRect();
                        if (rect.width > 100 && rect.height > 50) {
                            result.canvases.push({
                                index: i,
                                id: c.id || '',
                                className: c.className || '',
                                width: rect.width,
                                height: rect.height,
                                visible: rect.width > 0 && rect.height > 0,
                            });
                        }
                    });

                    // 自定义播放器（常见视频网站的播放器容器）
                    const playerPatterns = [
                        // B站
                        '.bpx-player-video-area',
                        '#bilibili-player',
                        // YouTube
                        '.html5-video-player',
                        '.ytd-player',
                        // 开源播放器
                        '.video-js',
                        '.jwplayer',
                        '.dplayer',
                        '.xgplayer',
                        '.plyr',
                        '.fluid-player',
                        // 腾讯系
                        '.tcplayer',
                        '.txp_player',
                        '[class*="tencent"] [class*="player"]',
                        // 阿里系
                        '.prism-player',
                        // 小鹅通 / 知识付费平台
                        '[class*="xiaoetong"] [class*="player"]',
                        '[class*="player-container"]',
                        '[class*="video-wrap"]',
                        '[class*="player-wrapper"]',
                        '[class*="course-player"]',
                        '[class*="lesson-player"]',
                        '[class*="live-player"]',
                        // 其他教育平台
                        '[class*="mukewang"] [class*="player"]',
                        '[class*="study"] [class*="video"]',
                        '[class*="edu"] [class*="player"]',
                        // 通用属性
                        '[data-player]',
                        '[data-video-id]',
                        '[data-video]',
                    ];
                    playerPatterns.forEach(sel => {
                        const el = document.querySelector(sel);
                        if (el) {
                            const rect = el.getBoundingClientRect();
                            // 查找内部是否有 video 标签
                            const innerVideo = el.querySelector('video');
                            result.custom_players.push({
                                selector: sel,
                                tagName: el.tagName.toLowerCase(),
                                width: rect.width,
                                height: rect.height,
                                hasVideoInside: !!innerVideo,
                                innerVideoSelector: innerVideo ? (
                                    innerVideo.id ? '#' + innerVideo.id :
                                    innerVideo.className ? '.' + innerVideo.className.split(' ')[0] :
                                    'video'
                                ) : null,
                            });
                        }
                    });

                    // 自动建议最佳选择器
                    result.suggestion = 'video';
                    if (result.custom_players.length > 0) {
                        const p = result.custom_players[0];
                        if (p.hasVideoInside && p.innerVideoSelector) {
                            // 如果播放器内部有 video，建议用播放器内的 video
                            const combined = p.selector + ' ' + p.innerVideoSelector;
                            result.suggestion = combined.replace(/^\./, '');  // 简化
                            result.suggestion_detail = combined;
                        }
                    }
                    if (result.videos.length > 0) {
                        // 优先用最底层的，避免 B 站等多余层
                        // B站: .bpx-player-video-area video 而不是全局 video
                        if (result.suggestion === 'video' && result.custom_players.length > 0) {
                            result.suggestion_detail = result.suggestion;
                        }
                    }

                    // 兜底启发式扫描：上面都没找到，搜"长得像播放器的大块区域"
                    if (result.custom_players.length === 0 && result.videos.length === 0) {
                        const allDivs = document.querySelectorAll('div, section');
                        allDivs.forEach(el => {
                            const rect = el.getBoundingClientRect();
                            // 播放器通常 > 400x250 且在视口内
                            if (rect.width < 400 || rect.height < 250) return;
                            if (rect.top < -200 || rect.top > window.innerHeight) return;
                            const cls = (el.className || '').toString().toLowerCase();
                            const id = (el.id || '').toLowerCase();
                            const hasKeyword = cls.includes('video') || cls.includes('player') ||
                                cls.includes('播放') || id.includes('video') || id.includes('player');
                            const hasVideoInside = !!el.querySelector('video, iframe, canvas, audio');
                            if (hasKeyword || hasVideoInside) {
                                const innerVideo = el.querySelector('video');
                                result.custom_players.push({
                                    selector: id ? '#' + id : '.' + cls.split(' ')[0],
                                    tagName: el.tagName.toLowerCase(),
                                    width: rect.width,
                                    height: rect.height,
                                    hasVideoInside: !!innerVideo,
                                    innerVideoSelector: innerVideo ? (
                                        innerVideo.id ? '#' + innerVideo.id :
                                        innerVideo.className ? '.' + innerVideo.className.split(' ')[0] :
                                        'video'
                                    ) : null,
                                    source: 'heuristic',
                                });
                            }
                        });
                    }

                    return result;
                }
            """)
            info["selector"] = self._video_selector  # 当前使用的选择器
            return info
        except Exception as e:
            return {"error": str(e)}

    def set_video_selector(self, selector: str):
        """设置自定义视频元素选择器（LLM 决定）。例: '.bpx-player-video-area video'"""
        self._video_selector = selector
        print(f"[Browser] 视频选择器已更新: {selector}")

    # ── 状态 ──

    async def get_state(self) -> VideoState | None:
        """获取当前视频播放状态。先确保 page 指向用户可见的标签页。"""
        await self.ensure_active_tab()
        if not self._page:
            return None
        return await _read_video_state(self._page, self._video_selector)

    async def get_current_time(self) -> float:
        """获取当前播放时间（秒）。"""
        if not self._page:
            return 0.0
        try:
            t = await self._page.evaluate(f"""
                document.querySelector('{self._video_selector}')?.currentTime ?? 0
            """)
            return float(t)
        except Exception:
            return 0.0

    async def get_duration(self) -> float:
        """获取视频总时长（秒）。"""
        if not self._page:
            return 0.0
        try:
            d = await self._page.evaluate(f"""
                document.querySelector('{self._video_selector}')?.duration ?? 0
            """)
            return float(d)
        except Exception:
            return 0.0

    # ── 操控 ──

    async def play(self):
        """播放。"""
        await _toggle_play(self._page, True, self._video_selector)

    async def pause(self):
        """暂停。"""
        await _toggle_play(self._page, False, self._video_selector)

    async def seek(self, target_time: float) -> dict:
        """跳转到指定时间。"""
        return await _seek_video(self._page, target_time, self._video_selector)

    async def toggle(self) -> bool:
        """切换播放/暂停，返回当前是否在播放。"""
        s = await self.get_state()
        if s is None:
            return False
        if s["paused"]:
            await self.play()
            return True
        else:
            await self.pause()
            return False

    # ── 截图 ──

    async def screenshot(self, save_path: str) -> str:
        """截取当前视频帧。"""
        if not self._page:
            raise RuntimeError("未定位到视频页面")
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        await self._page.screenshot(path=save_path, full_page=False)
        return save_path

    async def navigate(self, url: str, wait_spa: bool = True):
        """导航到指定 URL（在已连接的页面中打开）。

        wait_spa=True: 等待 SPA（Vue/React）渲染完成后再返回。
        """
        if not self._browser:
            raise RuntimeError("浏览器未连接")
        # 在现有页面导航
        if self._page:
            await self._page.goto(url, wait_until="domcontentloaded")
        else:
            ctx = self._browser.contexts[0] if self._browser.contexts else None
            if ctx:
                self._page = await ctx.new_page()
                await self._page.goto(url, wait_until="domcontentloaded")

        if wait_spa:
            await self._wait_for_spa_render()

    async def _wait_for_spa_render(self, timeout: float = 15.0) -> bool:
        """等待 SPA 页面渲染完成。检测 Vue/React 挂载点出现非空内容。"""
        if not self._page:
            return False
        try:
            await self._page.wait_for_function("""
                () => {
                    // 检查常见 SPA 挂载点是否有可见内容
                    const mounts = ['#app', '#root', '[data-app]', 'main', '#__next', '#__nuxt'];
                    for (const sel of mounts) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const rect = el.getBoundingClientRect();
                            const hasChildren = el.children.length > 0;
                            const hasText = (el.textContent || '').trim().length > 50;
                            if (rect.width > 0 && (hasChildren || hasText)) return true;
                        }
                    }
                    // 兜底：body 至少有一个大块内容
                    const body = document.body;
                    if (body && body.textContent.trim().length > 200) return true;
                    return false;
                }
            """, timeout=timeout * 1000)
            return True
        except Exception:
            # 超时也算了，不影响后续流程
            return False

    async def capture_batch(
        self,
        shots: list[dict],
        save_dir: str = "screenshots",
        restore_time: float | None = None,
    ) -> list[CaptureResult]:
        """
        批量截图 — 核心工具。

        shots: [{"time": 120.5, "name": "Transformer 架构图"}, ...]
        save_dir: 截图保存目录
        restore_time: 截图完成后跳回的时间（None=不跳回，传 0=回到开头）

        返回 CaptureResult 列表。
        """
        if not self._page:
            raise RuntimeError("未定位到视频页面")

        if restore_time is None:
            restore_time = await self.get_current_time()

        results: list[CaptureResult] = []

        for shot in shots:
            target_time = float(shot["time"])
            name = str(shot.get("name", ""))

            await _seek_video(self._page, target_time, self._video_selector)
            await asyncio.sleep(0.5)

            result = await _screenshot_video(
                self._page, save_dir, target_time, name, self._video_selector
            )
            results.append(result)

        if restore_time is not None:
            await _seek_video(self._page, restore_time, self._video_selector)
            await asyncio.sleep(0.3)

        return results

    async def get_page_structure(self, retry_until_content: bool = True) -> dict:
        """提取页面关键交互元素，供 AI 分析跳转入口、导航等。

        retry_until_content=True: 如果结果为空（SPA 未渲染完），等待并重试最多 3 次。
        每次调用前确保 page 指向用户可见的标签页。
        """
        await self.ensure_active_tab()
        if not self._page:
            return {"error": "未连接到任何页面", "elements": []}

        max_retries = 3 if retry_until_content else 1
        for attempt in range(max_retries):
            result = await self._page.evaluate("""
            (() => {
                const data = {
                    title: document.title || '',
                    url: location.href,
                    elements: []
                };

                // 辅助：获取元素文本（截断到80字符）
                const text = (el) => {
                    const t = (el.textContent || el.getAttribute('aria-label') || el.title || '').trim();
                    return t ? t.replace(/\\s+/g, ' ').substring(0, 80) : '';
                };

                // 辅助：获取元素的 CSS 选择器（简化）
                const css = (el) => {
                    if (el.id) return '#' + el.id;
                    const cls = (el.className || '').toString().trim().split(/\\s+/).slice(0, 2).join('.');
                    const tag = el.tagName.toLowerCase();
                    return cls ? tag + '.' + cls : tag;
                };

                // 辅助：判定元素是否可见
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };

                // 1. 导航区域链接
                const navAreas = document.querySelectorAll('nav, header, [class*="nav"], [class*="header"], [class*="menu"], [class*="sidebar"], [class*="left-panel"]');
                navAreas.forEach(area => {
                    area.querySelectorAll('a[href]').forEach(a => {
                        if (!visible(a)) return;
                        const t = text(a);
                        const href = a.href || a.getAttribute('href') || '';
                        if (t && href && !href.startsWith('javascript:') && !href.startsWith('#')) {
                            data.elements.push({type: 'nav_link', text: t, href: href, selector: css(a)});
                        }
                    });
                });

                // 2. 按钮 / 可点击元素
                const clickables = document.querySelectorAll('button, [role="button"], [class*="btn"], [onclick], input[type="submit"], input[type="button"]');
                clickables.forEach(el => {
                    if (!visible(el)) return;
                    const t = text(el);
                    if (t) {
                        const href = el.tagName === 'A' ? (el.href || '') : '';
                        data.elements.push({type: 'button', text: t, href: href, selector: css(el)});
                    }
                });

                // 3. 视频相关元素（推荐列表、播放列表、下一集按钮等）
                const videoAreas = document.querySelectorAll(
                    '[class*="video"], [class*="playlist"], [class*="recommend"], [class*="related"], ' +
                    '[class*="episode"], [class*="chapter"], [class*="collection"], [class*="card"], ' +
                    '[class*="list"] a[href], [class*="suggest"] a[href]'
                );
                const seenHrefs = new Set();
                videoAreas.forEach(el => {
                    const t = text(el);
                    const href = el.href || el.getAttribute('href') || '';
                    if (t && href && !href.startsWith('javascript:') && !seenHrefs.has(href)) {
                        seenHrefs.add(href);
                        data.elements.push({type: 'video_related', text: t, href: href, selector: css(el)});
                    }
                });

                // 4. 搜索框
                const searchBoxes = document.querySelectorAll('input[type="search"], input[placeholder*="搜"], input[placeholder*="search"], input[name*="search"], input[name*="keyword"], input[id*="search"], input[class*="search"]');
                searchBoxes.forEach(el => {
                    if (!visible(el)) return;
                    const ph = el.getAttribute('placeholder') || '';
                    data.elements.push({type: 'search_box', text: ph, selector: css(el)});
                });

                // 5. 主要内容区域链接（可能是教程目录、课程列表等）
                const mainAreas = document.querySelectorAll('main, [class*="content"], [class*="main"], article, [class*="article"]');
                mainAreas.forEach(area => {
                    area.querySelectorAll('a[href]').forEach(a => {
                        if (!visible(a)) return;
                        const t = text(a);
                        const href = a.href || a.getAttribute('href') || '';
                        if (t && href && !href.startsWith('javascript:') && !href.startsWith('#') && !seenHrefs.has(href)) {
                            seenHrefs.add(href);
                            data.elements.push({type: 'content_link', text: t, href: href, selector: css(a)});
                        }
                    });
                });

                // 去重 + 限制总数
                const seen = new Set();
                data.elements = data.elements.filter(e => {
                    const key = e.text + e.href;
                    if (seen.has(key)) return false;
                    seen.add(key);
                    return true;
                }).slice(0, 80);  // 最多 80 个元素

                return data;
            })()
            """)
            # 如果结果有内容，或不需要重试，直接返回
            if not retry_until_content or len(result.get("elements", [])) >= 3:
                return result
            # SPA 可能还没渲染完——等 2 秒再试
            if attempt < max_retries - 1:
                print(f"[Browser] 页面元素为空（SPA 可能未渲染），第 {attempt+1} 次重试...")
                await asyncio.sleep(2)
        return result

    async def get_page_ax_tree(self, max_items: int = 80) -> dict:
        """用 Playwright accessibility snapshot（底层 CDP Accessibility.snapshotAX）
        提取页面交互元素。比 JS DOM 查询更通用、更高效，不依赖 CSS 选择器。

        返回格式与 get_page_structure() 兼容，DS 可以无缝切换。
        """
        await self.ensure_active_tab()
        if not self._page:
            return {"error": "未连接到任何页面", "elements": []}

        ax_root = await self._page.accessibility.snapshot()
        if not ax_root:
            return {"title": "", "url": "", "elements": []}

        # 收集页面基本信息
        title = ""
        url = ""
        try:
            title = await self._page.title()
            url = self._page.url
        except Exception:
            pass

        # 递归展平 AX 树，只取交互元素
        elements: list[dict] = []
        seen_texts: set[str] = set()

        def _flatten(node: dict, depth: int = 0):
            if depth > 20:  # 防止无限递归
                return
            role = (node.get("role") or "").lower()
            name = (node.get("name") or "").strip()
            value = node.get("value", "")

            # 只收集有意义的交互元素
            if role in ("link", "button", "menuitem", "tab", "listitem", "option"):
                if name and name not in seen_texts:
                    seen_texts.add(name)
                    elem = {"type": role, "text": name[:60]}
                    if role == "link" and value:
                        elem["href"] = str(value)
                    elements.append(elem)

            elif role in ("textbox", "searchbox", "combobox"):
                ph = (node.get("placeholder") or name or "").strip()
                if ph and ph not in seen_texts:
                    seen_texts.add(ph)
                    elements.append({"type": "search_box", "text": ph[:60]})

            elif role == "heading" and name:
                if name not in seen_texts:
                    seen_texts.add(name)
                    elements.append({"type": "heading", "text": name[:60]})

            # 递归子节点
            for child in node.get("children", []) or []:
                if len(elements) < max_items * 2:  # 放宽收集上限，后续再排序裁剪
                    _flatten(child, depth + 1)

        _flatten(ax_root)

        # 去重 + 排序（优先 link/button，其次其他）+ 限制总数
        priority_order = {"link": 0, "button": 1, "search_box": 2, "heading": 3,
                          "menuitem": 4, "tab": 5, "listitem": 6, "option": 7}
        result = sorted(elements, key=lambda e: priority_order.get(e["type"], 8))
        result = result[:max_items]

        return {
            "title": title,
            "url": url,
            "elements": result,
            "source": "ax_tree",  # 标记数据来源
        }

    # ── 页面交互（点击）──

    async def click_element(
        self,
        text: str | None = None,
        selector: str | None = None,
        index: int = 0,
    ) -> bool:
        """点击页面上的元素。

        Args:
            text:     按可见文本匹配（优先用这个，DS 从 AX tree 拿到文本后直接点）
            selector: CSS 选择器（精确控制）
            index:    匹配第几个（text 匹配到多个时用）

        Returns:
            True 如果成功点击，False 如果未找到元素
        """
        if not self._page:
            return False

        try:
            await self.ensure_active_tab()
        except Exception:
            pass

        try:
            if text:
                # 文本匹配：优先精确匹配，回退模糊匹配
                locator = self._page.get_by_text(text, exact=True)
                count = await locator.count()
                if count == 0:
                    locator = self._page.get_by_text(text, exact=False)
                    count = await locator.count()

                if count > 0:
                    target = locator.nth(min(index, count - 1))
                    await target.scroll_into_view_if_needed()
                    await target.click(timeout=5000)
                    print(f"[Browser] 点击成功 (text='{text[:30]}', idx={index})")
                    return True
                else:
                    print(f"[Browser] 未找到文本为 '{text[:30]}' 的可点击元素")

            if selector:
                locator = self._page.locator(selector)
                count = await locator.count()
                if count > 0:
                    target = locator.nth(min(index, count - 1))
                    await target.scroll_into_view_if_needed()
                    await target.click(timeout=5000)
                    print(f"[Browser] 点击成功 (selector='{selector[:60]}')")
                    return True
                else:
                    print(f"[Browser] 未找到选择器 '{selector[:60]}'")

        except Exception as e:
            # 普通点击失败 → 尝试 JS click（绕过遮罩层/不可见等限制）
            print(f"[Browser] 普通点击失败: {e}，尝试 JS click...")
            try:
                if text:
                    js = f"""
                        (() => {{
                            const els = [...document.querySelectorAll('*')];
                            const match = els.find(el =>
                                el.textContent.trim().includes({json.dumps(text)}) &&
                                el.offsetParent !== null &&
                                !el.disabled
                            );
                            if (match) {{ match.click(); return true; }}
                            return false;
                        }})()
                    """
                    clicked = await self._page.evaluate(js)
                    if clicked:
                        print(f"[Browser] JS 点击成功 (text='{text[:30]}')")
                        return True
            except Exception as e2:
                print(f"[Browser] 点击最终失败: {e2}")

        return False
