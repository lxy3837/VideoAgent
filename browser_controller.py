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
import os
import re
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TypedDict

try:
    from playwright.async_api import async_playwright, Browser, Page
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


CDP_DEFAULT_PORT = 9222
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
    # 尝试从注册表找
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


def is_cdp_running(port: int = CDP_DEFAULT_PORT) -> bool:
    """检查 CDP 端口是否已被监听。"""
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def start_edge_with_cdp(port: int = CDP_DEFAULT_PORT, user_data_dir: str | None = None) -> subprocess.Popen | None:
    """
    以远程调试模式启动 Edge。
    使用独立 user-data-dir 避免和当前运行的 Edge 冲突。

    返回 subprocess.Popen，失败返回 None。
    """
    edge_path = _find_edge()
    if not edge_path:
        print("[Browser] 找不到 Edge，请确认已安装")
        return None

    if user_data_dir is None:
        user_data_dir = str(Path(os.environ.get("TEMP", ".")) / "video_agent_edge_cdp")

    os.makedirs(user_data_dir, exist_ok=True)

    cmd = [
        edge_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window", "about:blank",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        print(f"[Browser] Edge 已启动 (pid={proc.pid}, cd localhost:{port})")
        return proc
    except Exception as e:
        print(f"[Browser] 启动 Edge 失败: {e}")
        return None


# ── 页面查找 ────────────────────────────────────────────

async def _find_video_page(browser: Browser) -> Page | None:
    """
    在所有页面中查找包含 <video> 标签的页面。
    返回第一个找到的页面，没有则返回 None。
    """
    pages = browser.contexts[0].pages if browser.contexts else []
    for page in pages:
        try:
            has_video = await page.evaluate(
                "() => !!document.querySelector('video')"
            )
            if has_video:
                return page
        except Exception:
            continue
    return None


async def _get_all_pages(browser: Browser) -> list[dict]:
    """列出浏览器中所有页面，供 GUI 选择。"""
    result = []
    if not browser.contexts:
        return result
    for ctx in browser.contexts:
        for page in ctx.pages:
            try:
                title = await page.title()
                url = page.url
                has_video = await page.evaluate(
                    "() => !!document.querySelector('video')"
                )
                result.append({
                    "title": title,
                    "url": url,
                    "has_video": has_video,
                })
            except Exception:
                result.append({
                    "title": "(无法访问)",
                    "url": page.url,
                    "has_video": False,
                })
    return result


# ── 视频状态 ────────────────────────────────────────────

async def _read_video_state(page: Page) -> VideoState | None:
    """从页面读取视频播放状态（精确到毫秒）。"""
    try:
        state = await page.evaluate("""
            () => {
                const v = document.querySelector('video');
                if (!v) return null;
                return {
                    current_time: v.currentTime,
                    duration: v.duration || 0,
                    paused: v.paused,
                    playback_rate: v.playbackRate,
                    video_width: v.videoWidth,
                    video_height: v.videoHeight,
                };
            }
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


async def _seek_video(page: Page, target_time: float) -> dict:
    """精确跳转到指定时间（秒）。"""
    return await _execute_video_action(page, f"""
        (() => {{
            const v = document.querySelector('video');
            if (!v) return {{error: 'no video'}};
            v.currentTime = {target_time};
            return {{currentTime: v.currentTime, paused: v.paused}};
        }})()
    """)


async def _toggle_play(page: Page, play: bool) -> dict:
    """播放或暂停。"""
    action = "v.play()" if play else "v.pause()"
    return await _execute_video_action(page, f"""
        (() => {{
            const v = document.querySelector('video');
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
) -> CaptureResult:
    """
    截取当前视频帧，保存为带语义名称的文件。
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

    await page.screenshot(path=filepath, full_page=False)

    return CaptureResult(
        time=video_time,
        name=name,
        path=filepath,
        width=0,  # 由调用方填充
        height=0,
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

    # ── 连接 / 断开 ──

    async def connect(self, auto_start: bool = True) -> bool:
        """
        连接 Edge CDP。
        如果端口未监听且 auto_start=True，自动启动 Edge。
        返回是否连接成功。
        """
        if not HAS_PLAYWRIGHT:
            print("[Browser] 缺少 playwright，请运行: pip install playwright && playwright install chromium")
            return False

        if not is_cdp_running(self._port):
            if auto_start:
                print("[Browser] CDP 端口未监听，正在启动 Edge...")
                self._proc = start_edge_with_cdp(self._port)
                # 等 Edge 启动
                for _ in range(30):
                    await asyncio.sleep(1)
                    if is_cdp_running(self._port):
                        break
                else:
                    print("[Browser] Edge 启动超时")
                    return False
            else:
                print(f"[Browser] CDP 端口 {self._port} 未监听，请先启动: msedge.exe --remote-debugging-port={self._port}")
                return False

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                f"http://localhost:{self._port}"
            )
            self._connected = True
            print(f"[Browser] 已连接 CDP (localhost:{self._port})")
            return True
        except Exception as e:
            print(f"[Browser] CDP 连接失败: {e}")
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

    # ── 页面管理 ──

    async def list_pages(self) -> list[dict]:
        """列出浏览器中所有标签页。"""
        if not self._browser:
            return []
        return await _get_all_pages(self._browser)

    async def select_video_page(self, page_url: str | None = None) -> bool:
        """
        定位到包含 <video> 标签的页面。
        若 page_url 指定，则精确匹配 URL 前缀；否则自动查找。

        返回是否找到。
        """
        if not self._browser:
            return False

        if page_url:
            # 精确匹配
            for ctx in self._browser.contexts:
                for page in ctx.pages:
                    if page.url.startswith(page_url):
                        self._page = page
                        return True

        # 自动查找
        self._page = await _find_video_page(self._browser)
        if self._page:
            print(f"[Browser] 定位到视频页: {await self._page.title()}")
            return True

        print("[Browser] 未找到含视频的标签页")
        return False

    # ── 状态 ──

    async def get_state(self) -> VideoState | None:
        """获取当前视频播放状态。"""
        if not self._page:
            return None
        return await _read_video_state(self._page)

    async def get_current_time(self) -> float:
        """获取当前播放时间（秒）。"""
        if not self._page:
            return 0.0
        try:
            t = await self._page.evaluate(
                "document.querySelector('video')?.currentTime ?? 0"
            )
            return float(t)
        except Exception:
            return 0.0

    async def get_duration(self) -> float:
        """获取视频总时长（秒）。"""
        if not self._page:
            return 0.0
        try:
            d = await self._page.evaluate(
                "document.querySelector('video')?.duration ?? 0"
            )
            return float(d)
        except Exception:
            return 0.0

    # ── 操控 ──

    async def play(self):
        """播放。"""
        await _toggle_play(self._page, True)

    async def pause(self):
        """暂停。"""
        await _toggle_play(self._page, False)

    async def seek(self, target_time: float) -> dict:
        """跳转到指定时间。"""
        return await _seek_video(self._page, target_time)

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

        # 记下当前位置
        if restore_time is None:
            restore_time = await self.get_current_time()

        results: list[CaptureResult] = []

        for shot in shots:
            target_time = float(shot["time"])
            name = str(shot.get("name", ""))

            # 跳转
            await _seek_video(self._page, target_time)
            await asyncio.sleep(0.5)  # 等画面稳定

            # 截图
            result = await _screenshot_video(
                self._page, save_dir, target_time, name
            )
            results.append(result)

        # 回到原位
        if restore_time is not None:
            await _seek_video(self._page, restore_time)
            await asyncio.sleep(0.3)

        return results
