#!/usr/bin/env python3
"""
实时扬声器字幕 - Live Caption Overlay
========================================
捕获系统扬声器输出 → Whisper 实时语音识别 → 悬浮字幕显示

功能:
  - 监听系统扬声器/音频输出，实时转写字幕
  - 半透明悬浮窗显示，类似视频内置字幕效果
  - 鼠标拖动字幕窗口任意调整位置
  - 滚轮调节字号 | 右键菜单退出 | Esc 退出

使用:
  python live_caption_video.py
"""

import queue
import sys
import os
from datetime import datetime
import tkinter as tk
from tkinter import font as tkfont

# ── 导入转录核心（共享模块） ──
from transcriber_core import (
    AudioCapture, Transcriber, check_dependencies,
)

# ── UI 默认参数 ───────────────────────────────────────────
DEFAULT_FONT_SIZE = 28
DEFAULT_WIN_WIDTH = 900
DEFAULT_WIN_HEIGHT = 200
DEFAULT_BG_ALPHA = 0.75


# ╔══════════════════════════════════════════════════════════╗
# ║              悬浮字幕窗口  (CaptionOverlay)              ║
# ╚══════════════════════════════════════════════════════════╝

class CaptionOverlay:
    """无边框、置顶、半透明的悬浮字幕窗口，可拖动。B站风格双行显示 + 描边字幕。"""

    GITHUB_URL = "https://github.com/lxy3837/live-caption"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Live Caption")

        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', DEFAULT_BG_ALPHA)
        self.root.configure(bg='#1a1a1a')

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - DEFAULT_WIN_WIDTH) // 2
        y = sh - DEFAULT_WIN_HEIGHT - 60
        self.root.geometry(f"{DEFAULT_WIN_WIDTH}x{DEFAULT_WIN_HEIGHT}+{x}+{y}")

        self.font_size = DEFAULT_FONT_SIZE
        self._font = tkfont.Font(
            family="Microsoft YaHei", size=self.font_size, weight="bold"
        )
        self._font_dim = tkfont.Font(
            family="Microsoft YaHei", size=self.font_size - 4, weight="normal"
        )

        self.prev_canvas = tk.Canvas(
            self.root, bg='#1a1a1a', highlightthickness=0, height=60,
        )
        self.prev_canvas.pack(fill="x", padx=10, pady=(8, 0))

        self.cur_canvas = tk.Canvas(
            self.root, bg='#1a1a1a', highlightthickness=0, height=80,
        )
        self.cur_canvas.pack(fill="x", padx=10, pady=(2, 0))

        self.gh_label = tk.Label(
            self.root,
            text=f"🔗 {self.GITHUB_URL}",
            font=tkfont.Font(family="Microsoft YaHei", size=9),
            fg="#444444", bg='#1a1a1a', cursor="hand2",
        )
        self.gh_label.pack(side="bottom", pady=(0, 4))
        self.gh_label.bind("<Button-1>", self._on_github_click)

        self._drag_start_x = 0
        self._drag_start_y = 0
        for widget in (self.prev_canvas, self.cur_canvas, self.gh_label, self.root):
            widget.bind("<Button-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_move)

        for widget in (self.prev_canvas, self.cur_canvas, self.gh_label, self.root):
            widget.bind("<MouseWheel>", self._on_mousewheel)

        self.root.bind("<Escape>", lambda e: self.close())

        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label="字号 +", command=self._increase_font)
        self._menu.add_command(label="字号 -", command=self._decrease_font)
        self._menu.add_separator()
        self._menu.add_command(label=f"GitHub ⭐", command=self._on_github_click)
        self._menu.add_separator()
        self._menu.add_command(label="退出 (Esc)", command=self.close)
        for widget in (self.prev_canvas, self.cur_canvas, self.gh_label, self.root):
            widget.bind("<Button-3>", self._show_menu)

        self._running = True
        self._model_ready = False

    def _draw_outline_text(self, canvas: tk.Canvas, text: str,
                           fill_color: str, outline_color: str, font: tkfont.Font):
        canvas.delete("all")
        if not text:
            return
        w = canvas.winfo_width() or DEFAULT_WIN_WIDTH
        pad = 20
        avg_char_w = font.measure("测")
        max_chars = max(1, (w - pad * 2) // avg_char_w)
        lines = self._wrap_text(text, max_chars)
        line_h = font.metrics("linespace")
        total_h = len(lines) * line_h
        y_start = max(0, (canvas.winfo_height() - total_h) // 2)
        offsets = [(-1, -1), (0, -1), (1, -1), (-1, 0),
                    (1, 0), (-1, 1), (0, 1), (1, 1)]
        for i, line in enumerate(lines):
            y = y_start + i * line_h + line_h // 2
            for dx, dy in offsets:
                canvas.create_text(w // 2 + dx, y + dy,
                                   text=line, font=font,
                                   fill=outline_color, anchor="center")
            canvas.create_text(w // 2, y,
                               text=line, font=font,
                               fill=fill_color, anchor="center")

    @staticmethod
    def _wrap_text(text: str, max_chars: int) -> list:
        raw_lines = text.split("\n")
        result = []
        for line in raw_lines:
            while len(line) > max_chars:
                cut = max_chars
                for sep in (" ", "，", "。", "、", "；", "：", "！", "？", ",", "."):
                    pos = line[:max_chars].rfind(sep)
                    if pos > max_chars // 2:
                        cut = pos + 1
                        break
                result.append(line[:cut])
                line = line[cut:].lstrip()
            if line:
                result.append(line)
        return result

    def _on_drag_start(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y

    def _on_drag_move(self, event):
        dx = event.x - self._drag_start_x
        dy = event.y - self._drag_start_y
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self.root.geometry(f"+{x}+{y}")

    def _on_mousewheel(self, event):
        delta = 1 if event.delta > 0 else -1
        new_size = self.font_size + delta
        if 12 <= new_size <= 80:
            self.font_size = new_size
            self._font.configure(size=self.font_size)
            self._font_dim.configure(size=max(10, self.font_size - 4))
            self._redraw()

    def _increase_font(self):
        if self.font_size < 80:
            self.font_size += 2
            self._font.configure(size=self.font_size)
            self._font_dim.configure(size=max(10, self.font_size - 4))
            self._redraw()

    def _decrease_font(self):
        if self.font_size > 12:
            self.font_size -= 2
            self._font.configure(size=self.font_size)
            self._font_dim.configure(size=max(10, self.font_size - 4))
            self._redraw()

    def _show_menu(self, event):
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _on_github_click(self, event=None):
        import webbrowser
        webbrowser.open(self.GITHUB_URL)

    def set_text(self, text: str):
        if not self._running:
            return
        self.root.after(0, self._apply_text, text)

    def _apply_text(self, text: str):
        try:
            if not self._model_ready:
                self._draw_outline_text(
                    self.cur_canvas, text,
                    fill_color="#ffffff", outline_color="#222222",
                    font=self._font,
                )
                if "监听" in text:
                    self._model_ready = True
            else:
                old_items = self.cur_canvas.find_all()
                old_text = ""
                if old_items:
                    old_text = self.cur_canvas.itemcget(old_items[-1], "text")
                if old_text and text and old_text != text:
                    self._draw_outline_text(
                        self.prev_canvas, old_text,
                        fill_color="#aaaaaa", outline_color="#222222",
                        font=self._font_dim,
                    )
                self._draw_outline_text(
                    self.cur_canvas, text,
                    fill_color="#ffffff", outline_color="#222222",
                    font=self._font,
                )
        except tk.TclError:
            pass

    def _redraw(self):
        try:
            items = self.prev_canvas.find_all()
            if items:
                txt = self.prev_canvas.itemcget(items[-1], "text")
                self._draw_outline_text(
                    self.prev_canvas, txt,
                    fill_color="#aaaaaa", outline_color="#222222",
                    font=self._font_dim,
                )
            items = self.cur_canvas.find_all()
            if items:
                txt = self.cur_canvas.itemcget(items[-1], "text")
                self._draw_outline_text(
                    self.cur_canvas, txt,
                    fill_color="#ffffff", outline_color="#222222",
                    font=self._font,
                )
        except tk.TclError:
            pass

    def close(self):
        self._running = False
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()


# ╔══════════════════════════════════════════════════════════╗
# ║                    主入口  (main)                       ║
# ╚══════════════════════════════════════════════════════════╝

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    save_path = os.path.join(
        script_dir, f"captions_{datetime.now().strftime('%Y%m%d')}.txt"
    )

    # ── Banner ──
    import re
    ESC = "\x1b"
    B = f"{ESC}[1;36m"
    W = f"{ESC}[1;37m"
    Y = f"{ESC}[1;33m"
    R = f"{ESC}[0m"
    BW = 62
    _ANSI = re.compile(r'\x1b\[[0-9;]*m')

    def pad(s, w):
        clean = _ANSI.sub('', s)
        vis = 0
        for c in clean:
            cp = ord(c)
            if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                    0xF900 <= cp <= 0xFAFF or 0x3000 <= cp <= 0x303F or
                    0xFF00 <= cp <= 0xFFEF):
                vis += 2
            else:
                vis += 1
        return s + " " * max(0, w - vis)

    short_gh = "github.com/lxy3837/live-caption"
    save_name = os.path.basename(save_path)

    print(f"""
{Y}╔{'═' * BW}╗
║{pad('', BW)}║{B}
║{pad('   ██╗     ██╗██╗   ██╗███████╗', BW)}║
║{pad('   ██║     ██║██║   ██║██╔════╝', BW)}║
║{pad('   ██║     ██║██║   ██║█████╗', BW)}║
║{pad('   ██║     ██║╚██╗ ██╔╝██╔══╝', BW)}║
║{pad('   ███████╗██║ ╚████╔╝ ███████╗', BW)}║
║{pad('   ╚══════╝╚═╝  ╚═══╝  ╚══════╝', BW)}║
║{pad('', BW)}║
║{pad(f'    {W}██████╗  █████╗ ██████╗ ████████╗██╗ ██████╗ ███╗   ██╗{B}', BW)}║
║{pad(f'    {W}██╔════╝ ██╔══██╗██╔══██╗╚══██╔══╝██║██╔═══██╗████╗  ██║{B}', BW)}║
║{pad(f'    {W}██║      ███████║██████╔╝   ██║   ██║██║   ██║██╔██╗ ██║{B}', BW)}║
║{pad(f'    {W}██║      ██╔══██║██╔═══╝    ██║   ██║██║   ██║██║╚██╗██║{B}', BW)}║
║{pad(f'    {W}╚██████╗ ██║  ██║██║        ██║   ██║╚██████╔╝██║ ╚████║{B}', BW)}║
║{pad(f'    {W} ╚═════╝ ╚═╝  ╚═╝╚═╝        ╚═╝   ╚═╝ ╚═════╝ ╚═╝  ╚═══╝{B}', BW)}║{Y}
║{pad('', BW)}║
║{pad(f'   {W}实时扬声器字幕{R} · AI {W}Whisper{R} 驱动', BW)}║
║{pad('', BW)}║
║{pad(f'  {W}拖动{R} → 移位置  │  {W}滚轮{R} → 调字号', BW)}║
║{pad(f'  {W}右键{R} → 菜单    │  {W}Esc{R}  → 退出', BW)}║
║{pad('', BW)}║
║{pad(f'  {W}Star →{R} {B}{short_gh}{R}', BW)}║
║{pad(f'  字幕记录 → {save_name}', BW)}║
║{pad('', BW)}║
╚{'═' * BW}╝{R}
""")

    if not check_dependencies():
        input("按回车键退出...")
        sys.exit(1)

    q = queue.Queue()

    overlay = CaptionOverlay()
    capture = AudioCapture(q)
    transcriber = Transcriber(q, overlay.set_text, save_path)

    capture.start()
    transcriber.start()

    try:
        overlay.run()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[系统] 正在退出...")
        capture.stop()
        transcriber.stop()
        print("[系统] 再见！")


if __name__ == "__main__":
    main()
