"""
Agent GUI — 透明悬浮聊天窗 + 实时日志 + 配置面板。

特性:
  - 半透明背景 (alpha 0.88)，始终置顶
  - 可拖动、可折叠
  - 聊天区（主区域）：系统消息 + 用户指令 + AI 回复
  - 日志区（底部）：技术细节、错误、进度
  - 顶部状态栏
  - 配置按钮 → 弹出配置面板（API Key 等）
"""

from __future__ import annotations

import time
import threading
import queue
from datetime import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext


# ── UI 参数 ─────────────────────────────────────────────

DEFAULT_W = 560
DEFAULT_H = 520
MINI_H = 30
BG_COLOR = "#1a1a1a"
FG_COLOR = "#cccccc"
ACCENT = "#4a9eff"
DIM_COLOR = "#666666"
ERROR_COLOR = "#ff5555"
SUCCESS_COLOR = "#55cc55"
WARN_COLOR = "#ffcc55"
FONT_FAMILY = "Microsoft YaHei"
FONT_SIZE = 10
TITLE_SIZE = 11
ALPHA = 0.88
LOG_PROPORTION = 0.28   # 日志区占主区域的比例


class AgentGUI:
    """
    悬浮 GUI 窗口。

    用法:
        gui = AgentGUI(
            on_user_message=lambda text: ...,  # 聊天消息回调
            on_close=lambda: ...,              # 关闭回调
            on_config_save=lambda key: ...,    # 配置保存回调
            get_api_key=lambda: "sk-xxx",      # 获取当前 API Key
        )
        gui.start()
        gui.log("浏览器已连接")
        gui.assistant_say("分析完成")
        gui.set_status({"connected": True, ...})
    """

    def __init__(
        self,
        on_user_message=None,   # (text: str) -> None
        on_close=None,          # () -> None
        on_config_save=None,    # (api_key: str) -> bool
        get_api_key=None,       # () -> str
    ):
        self._on_user_message = on_user_message
        self._on_close = on_close
        self._on_config_save = on_config_save
        self._get_api_key = get_api_key

        self._msg_queue = queue.Queue()
        self._running = False

        self._root: tk.Tk | None = None
        self._collapsed = False
        self._drag_x = 0
        self._drag_y = 0
        self._on_ready = None

    # ═══════════════════════════════════════════════════════
    #  构建
    # ═══════════════════════════════════════════════════════

    def _build(self):
        self._root = tk.Tk()
        self._root.title("VideoAgent")
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.attributes("-alpha", ALPHA)
        self._root.configure(bg=BG_COLOR)

        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x = sw - DEFAULT_W - 20
        y = sh - DEFAULT_H - 60
        self._root.geometry(f"{DEFAULT_W}x{DEFAULT_H}+{x}+{y}")

        # ── 标题栏 ──
        self._title_frame = tk.Frame(self._root, bg="#111111", height=30)
        self._title_frame.pack(fill="x")
        self._title_frame.pack_propagate(False)

        self._title_label = tk.Label(
            self._title_frame, text="  🎬 VideoAgent",
            font=(FONT_FAMILY, TITLE_SIZE, "bold"),
            fg=ACCENT, bg="#111111", anchor="w",
        )
        self._title_label.pack(side="left", fill="both", expand=True)

        # 配置按钮
        self._config_btn = tk.Label(
            self._title_frame, text="⚙", font=(FONT_FAMILY, 14),
            fg=DIM_COLOR, bg="#111111", cursor="hand2", padx=6,
        )
        self._config_btn.pack(side="right")
        self._config_btn.bind("<Button-1>", self._open_config)
        self._config_btn.bind("<Enter>", lambda e: self._config_btn.configure(fg=ACCENT))
        self._config_btn.bind("<Leave>", lambda e: self._config_btn.configure(fg=DIM_COLOR))

        # 折叠按钮
        self._fold_btn = tk.Label(
            self._title_frame, text="─", font=(FONT_FAMILY, 14),
            fg=DIM_COLOR, bg="#111111", cursor="hand2", padx=6,
        )
        self._fold_btn.pack(side="right")
        self._fold_btn.bind("<Button-1>", self._on_fold)

        # 关闭按钮
        close_btn = tk.Label(
            self._title_frame, text="✕", font=(FONT_FAMILY, 12),
            fg=DIM_COLOR, bg="#111111", cursor="hand2", padx=8,
        )
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", self._on_close_click)
        close_btn.bind("<Enter>", lambda e: close_btn.configure(fg=ERROR_COLOR))
        close_btn.bind("<Leave>", lambda e: close_btn.configure(fg=DIM_COLOR))

        # 拖动
        drag_targets = (self._title_frame, self._title_label, self._fold_btn,
                         self._config_btn, close_btn)
        for w in drag_targets:
            w.bind("<Button-1>", self._on_drag_start)
            w.bind("<B1-Motion>", self._on_drag_move)

        # ── 主容器 ──
        self._main_frame = tk.Frame(self._root, bg=BG_COLOR)
        self._main_frame.pack(fill="both", expand=True)

        # ── 状态栏 ──
        self._status_frame = tk.Frame(self._main_frame, bg="#222222", height=24)
        self._status_frame.pack(fill="x")
        self._status_frame.pack_propagate(False)

        self._status_browser = tk.Label(
            self._status_frame, text="● 待机", font=(FONT_FAMILY, 9),
            fg=DIM_COLOR, bg="#222222", padx=6,
        )
        self._status_browser.pack(side="left")

        self._status_caption = tk.Label(
            self._status_frame, text="", font=(FONT_FAMILY, 9),
            fg=DIM_COLOR, bg="#222222", padx=6,
        )
        self._status_caption.pack(side="left")

        self._status_time = tk.Label(
            self._status_frame, text="", font=(FONT_FAMILY, 9),
            fg=DIM_COLOR, bg="#222222", padx=6,
        )
        self._status_time.pack(side="right")

        # ── 聊天标签 ──
        chat_label = tk.Label(
            self._main_frame, text="  💬 聊天", font=(FONT_FAMILY, 9),
            fg=DIM_COLOR, bg=BG_COLOR, anchor="w",
        )
        chat_label.pack(fill="x", padx=4, pady=(2, 0))

        # ═══════════════════════════════════════════════════
        #  关键: 先 pack 侧=bottom 的固定高度内容，它们抢占底部空间
        #  再 pack 侧=top 的 _chat_text(expand=True)，它填满中间
        # ═══════════════════════════════════════════════════

        # 日志区：从底部往上堆 (side=bottom 逆序)
        self._log_text = scrolledtext.ScrolledText(
            self._main_frame,
            font=(FONT_FAMILY, FONT_SIZE - 1),
            bg=BG_COLOR, fg=FG_COLOR,
            insertbackground=FG_COLOR,
            wrap=tk.WORD,
            state="disabled",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            height=6,
        )
        self._log_text.pack(side="bottom", fill="x", padx=4, pady=(0, 4))

        log_label = tk.Label(
            self._main_frame, text="  📋 日志", font=(FONT_FAMILY, 9),
            fg=DIM_COLOR, bg=BG_COLOR, anchor="w",
        )
        log_label.pack(side="bottom", fill="x", padx=4, pady=(0, 0))

        log_sep = tk.Frame(self._main_frame, bg="#333333", height=1)
        log_sep.pack(side="bottom", fill="x", padx=4, pady=(2, 4))

        # 输入区：放在日志上方 (相对于日志是 side=bottom 的下一个)
        input_frame = tk.Frame(self._main_frame, bg=BG_COLOR)
        input_frame.pack(side="bottom", fill="x", padx=4, pady=(0, 6))

        self._input_var = tk.StringVar()
        self._input_entry = tk.Entry(
            input_frame,
            textvariable=self._input_var,
            font=(FONT_FAMILY, FONT_SIZE),
            bg="#2a2a2a", fg="white",
            insertbackground="white",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#444444",
            highlightcolor=ACCENT,
        )
        self._input_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self._input_entry.bind("<Return>", self._on_send)
        self._input_entry.focus_set()

        send_btn = tk.Label(
            input_frame, text=" 发送 ",
            font=(FONT_FAMILY, FONT_SIZE, "bold"),
            fg=ACCENT, bg="#2a2a2a", cursor="hand2",
            padx=14, pady=5,
            highlightthickness=1, highlightbackground="#444444",
        )
        send_btn.pack(side="right", padx=(6, 0))
        send_btn.bind("<Button-1>", self._on_send)

        # 聊天区：最后 pack (默认 side=top) 填满中间剩余空间
        self._chat_text = scrolledtext.ScrolledText(
            self._main_frame,
            font=(FONT_FAMILY, FONT_SIZE),
            bg="#0d0d0d", fg=FG_COLOR,
            insertbackground=FG_COLOR,
            wrap=tk.WORD,
            state="disabled",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        self._chat_text.pack(fill="both", expand=True, padx=4, pady=2)

        self._chat_text.tag_config("user", foreground=ACCENT, font=(FONT_FAMILY, FONT_SIZE, "bold"))
        self._chat_text.tag_config("assistant", foreground=SUCCESS_COLOR)
        self._chat_text.tag_config("system", foreground=WARN_COLOR)
        self._chat_text.tag_config("dim", foreground=DIM_COLOR)

        self._log_text.tag_config("dim", foreground=DIM_COLOR)
        self._log_text.tag_config("success", foreground=SUCCESS_COLOR)
        self._log_text.tag_config("error", foreground=ERROR_COLOR)
        self._log_text.tag_config("warn", foreground=WARN_COLOR)
        self._log_text.tag_config("accent", foreground=ACCENT)

        # ── 右键菜单 ──
        self._menu = tk.Menu(self._root, tearoff=0, bg="#2a2a2a", fg=FG_COLOR,
                              font=(FONT_FAMILY, 9))
        self._menu.add_command(label="⚙ 配置", command=self._open_config)
        self._menu.add_separator()
        self._menu.add_command(label="折叠/展开", command=self._toggle_collapse)
        self._menu.add_separator()
        self._menu.add_command(label="清空日志", command=self._clear_log)
        self._menu.add_command(label="清空聊天", command=self._clear_chat)
        self._menu.add_separator()
        self._menu.add_command(label="退出", command=self._on_close_click)

        for w in (self._title_frame, self._title_label, self._main_frame,
                  self._log_text, self._chat_text):
            w.bind("<Button-3>", self._show_menu)

        # 消息轮询
        self._root.after(100, self._poll_messages)

        # on_ready 回调
        if self._on_ready:
            self._root.after(500, self._on_ready)

    # ═══════════════════════════════════════════════════════
    #  配置面板
    # ═══════════════════════════════════════════════════════

    def _open_config(self, event=None):
        """打开配置对话框。"""
        dialog = tk.Toplevel(self._root, bg="#1e1e1e")
        dialog.title("配置")
        dialog.geometry("440x220")
        dialog.resizable(False, False)
        dialog.transient(self._root)
        dialog.attributes("-topmost", True)

        # 居中
        dialog.update_idletasks()
        rx = self._root.winfo_x() + (self._root.winfo_width() - 440) // 2
        ry = self._root.winfo_y() + (self._root.winfo_height() - 220) // 2
        dialog.geometry(f"+{rx}+{ry}")

        # ── 深色主题内部容器 ──
        inner = tk.Frame(dialog, bg="#1e1e1e", padx=20, pady=16)
        inner.pack(fill="both", expand=True)

        tk.Label(
            inner, text="⚙ 配置", font=(FONT_FAMILY, 14, "bold"),
            fg=ACCENT, bg="#1e1e1e",
        ).pack(anchor="w", pady=(0, 12))

        # API Key
        tk.Label(
            inner, text="DeepSeek API Key", font=(FONT_FAMILY, 10),
            fg=FG_COLOR, bg="#1e1e1e",
        ).pack(anchor="w")

        key_var = tk.StringVar()
        current_key = self._get_api_key() if self._get_api_key else ""
        key_var.set(current_key)

        key_frame = tk.Frame(inner, bg="#1e1e1e")
        key_frame.pack(fill="x", pady=(4, 10))

        key_entry = tk.Entry(
            key_frame,
            textvariable=key_var,
            font=(FONT_FAMILY, 10),
            bg="#2a2a2a", fg="white",
            insertbackground="white",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#444444",
            show="•" if current_key else "",
        )
        key_entry.pack(side="left", fill="x", expand=True, ipady=4)

        # 显示/隐藏按钮
        show_var = tk.BooleanVar(value=not bool(current_key))
        def _toggle_show():
            key_entry.configure(show="" if show_var.get() else "•")
        show_cb = tk.Checkbutton(
            key_frame, text="显示", variable=show_var, command=_toggle_show,
            bg="#1e1e1e", fg=DIM_COLOR, selectcolor="#1e1e1e",
            activebackground="#1e1e1e", activeforeground=FG_COLOR,
            font=(FONT_FAMILY, 9),
        )
        show_cb.pack(side="right", padx=(8, 0))

        # 提示
        tk.Label(
            inner, text="获取 Key: https://platform.deepseek.com/api_keys",
            font=(FONT_FAMILY, 8), fg=DIM_COLOR, bg="#1e1e1e",
        ).pack(anchor="w")

        # 状态提示
        status_var = tk.StringVar()
        status_label = tk.Label(
            inner, textvariable=status_var, font=(FONT_FAMILY, 9),
            fg=SUCCESS_COLOR, bg="#1e1e1e",
        )
        status_label.pack(anchor="w", pady=(6, 0))

        # 按钮
        btn_frame = tk.Frame(inner, bg="#1e1e1e")
        btn_frame.pack(fill="x", pady=(12, 0))

        def _save():
            key = key_var.get().strip()
            if self._on_config_save:
                ok = self._on_config_save(key)
                if ok:
                    status_var.set("✓ 已保存")
                    status_label.configure(fg=SUCCESS_COLOR)
                    dialog.after(800, dialog.destroy)
                else:
                    status_var.set("保存失败")
                    status_label.configure(fg=ERROR_COLOR)
            else:
                dialog.destroy()

        tk.Label(
            btn_frame, text=" 保存 ", font=(FONT_FAMILY, 10, "bold"),
            fg=ACCENT, bg="#2a2a2a", cursor="hand2",
            padx=20, pady=6,
            highlightthickness=1, highlightbackground="#444444",
        ).pack(side="right", padx=(6, 0))
        btn_frame.winfo_children()[-1].bind("<Button-1>", lambda e: _save())

        tk.Label(
            btn_frame, text=" 取消 ", font=(FONT_FAMILY, 10),
            fg=DIM_COLOR, bg="#2a2a2a", cursor="hand2",
            padx=20, pady=6,
            highlightthickness=1, highlightbackground="#444444",
        ).pack(side="right")
        btn_frame.winfo_children()[-1].bind("<Button-1>", lambda e: dialog.destroy())

        key_entry.focus_set()
        dialog.grab_set()

    # ═══════════════════════════════════════════════════════
    #  公开方法（线程安全）
    # ═══════════════════════════════════════════════════════

    def log(self, msg: str, tag: str = "dim", with_time: bool = True):
        self._msg_queue.put(("log", msg, tag, with_time))

    def assistant_say(self, msg: str):
        self._msg_queue.put(("assistant", msg, None, None))

    def system_say(self, msg: str):
        self._msg_queue.put(("system", msg, None, None))

    def set_status(self, status: dict):
        self._msg_queue.put(("status", status, None, None))

    def set_title(self, title: str):
        self._msg_queue.put(("title", title, None, None))

    def start(self):
        self._build()
        self._running = True
        self.log("VideoAgent GUI 已启动", "accent")
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            pass

    def stop(self):
        self._running = False
        if self._root:
            try:
                self._root.destroy()
            except tk.TclError:
                pass

    # ═══════════════════════════════════════════════════════
    #  内部
    # ═══════════════════════════════════════════════════════

    def _poll_messages(self):
        try:
            while True:
                msg = self._msg_queue.get_nowait()
                self._handle_msg(msg)
        except queue.Empty:
            pass
        if self._running and self._root:
            self._root.after(100, self._poll_messages)

    def _handle_msg(self, msg):
        msg_type = msg[0]
        if msg_type == "log":
            _, text, tag, with_time = msg
            self._append_log(text, tag, with_time)
        elif msg_type in ("assistant", "system"):
            self._append_chat(msg[1], msg_type)
        elif msg_type == "status":
            self._update_status(msg[1])
        elif msg_type == "title":
            self._set_title_text(msg[1])

    def _append_log(self, text: str, tag: str = "dim", with_time: bool = True):
        if not self._log_text:
            return
        ts = datetime.now().strftime("%H:%M:%S") if with_time else ""
        line = f"[{ts}] {text}\n" if ts else f"{text}\n"
        self._log_text.configure(state="normal")
        self._log_text.insert(tk.END, line, tag)
        self._log_text.see(tk.END)
        self._log_text.configure(state="disabled")

    def _append_chat(self, text: str, who: str = "assistant"):
        if not self._chat_text:
            return
        label = "🤖 AI" if who == "assistant" else "📢"
        tag = who
        self._chat_text.configure(state="normal")
        self._chat_text.insert(tk.END, f"{label}: ", "dim")
        self._chat_text.insert(tk.END, f"{text}\n\n", tag)
        self._chat_text.see(tk.END)
        self._chat_text.configure(state="disabled")

    def _append_chat_user(self, text: str):
        if not self._chat_text:
            return
        self._chat_text.configure(state="normal")
        self._chat_text.insert(tk.END, "👤 你: ", "dim")
        self._chat_text.insert(tk.END, f"{text}\n\n", "user")
        self._chat_text.see(tk.END)
        self._chat_text.configure(state="disabled")

    def _update_status(self, status: dict):
        conn = status.get("connected", False)
        analyzing = status.get("analyzing", False)

        if analyzing:
            self._status_browser.configure(text="● 分析中", fg=ACCENT)
        elif conn:
            self._status_browser.configure(text="● 已连接", fg=SUCCESS_COLOR)
        else:
            self._status_browser.configure(text="● 待机", fg=DIM_COLOR)

        cap = status.get("caption_running", False)
        cap_time = status.get("caption_time", 0)
        self._status_caption.configure(
            text=f"🎤 字幕 {cap_time:.0f}s" if cap else "",
            fg=SUCCESS_COLOR if cap else DIM_COLOR,
        )

        vtime = status.get("video_time", 0)
        dur = status.get("duration", 0)
        if dur > 0:
            self._status_time.configure(text=f"⏱ {vtime:.0f}/{dur:.0f}s", fg=FG_COLOR)
        elif vtime > 0:
            self._status_time.configure(text=f"⏱ {vtime:.0f}s", fg=FG_COLOR)

    def _set_title_text(self, title: str):
        if self._title_label:
            self._title_label.configure(text=f"  🎬 {title}")

    def _clear_log(self):
        if self._log_text:
            self._log_text.configure(state="normal")
            self._log_text.delete("1.0", tk.END)
            self._log_text.configure(state="disabled")

    def _clear_chat(self):
        if self._chat_text:
            self._chat_text.configure(state="normal")
            self._chat_text.delete("1.0", tk.END)
            self._chat_text.configure(state="disabled")

    # ═══════════════════════════════════════════════════════
    #  事件
    # ═══════════════════════════════════════════════════════

    def _on_send(self, event=None):
        text = self._input_var.get().strip()
        if not text:
            return
        self._input_var.set("")
        self._append_chat_user(text)
        if self._on_user_message:
            self._on_user_message(text)

    def _on_close_click(self, event=None):
        if self._on_close:
            self._on_close()
        self.stop()

    def _on_fold(self, event=None):
        self._toggle_collapse()

    def _toggle_collapse(self):
        if self._collapsed:
            self._main_frame.pack(fill="both", expand=True)
            self._root.geometry(f"{DEFAULT_W}x{DEFAULT_H}")
            self._fold_btn.configure(text="─")
            self._collapsed = False
        else:
            self._main_frame.pack_forget()
            self._root.geometry(f"{DEFAULT_W}x{MINI_H}")
            self._fold_btn.configure(text="□")
            self._collapsed = True

    def _on_drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag_move(self, event):
        dx = event.x - self._drag_x
        dy = event.y - self._drag_y
        self._root.geometry(f"+{self._root.winfo_x() + dx}+{self._root.winfo_y() + dy}")

    def _show_menu(self, event):
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()
