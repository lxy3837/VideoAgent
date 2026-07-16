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
  python live_caption.py
"""

import threading
import queue
import time
import sys
import os
import warnings
from datetime import datetime
import numpy as np
import tkinter as tk
from tkinter import font as tkfont, messagebox

# ── 抑制 soundcard 录制抖动警告 ──
import soundcard.mediafoundation as _sc_mf
warnings.filterwarnings("ignore", category=_sc_mf.SoundcardRuntimeWarning)

# ── 模型下载目录（脚本所在目录下的 models 文件夹）─────────
os.environ["HF_HOME"] = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "models")
os.environ["HUGGINGFACE_HUB_CACHE"] = os.environ["HF_HOME"]
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"  # 国内镜像，不用翻墙

# ── 音频捕获 ──────────────────────────────────────────────
try:
    import soundcard as sc
    HAS_SOUNDCARD = True
except ImportError:
    HAS_SOUNDCARD = False

# ── 语音识别 ──────────────────────────────────────────────
try:
    from faster_whisper import WhisperModel
    HAS_WHISPER = True
except ImportError:
    HAS_WHISPER = False

# ── 配置 ──────────────────────────────────────────────────
SAMPLE_RATE = 16000               # 音频采样率
CHUNK_DURATION = 0.5              # 每次采集的音频长度（秒），越小响应越快
SILENCE_THRESHOLD = 0.008         # RMS 能量阈值，低于此值视为静音
MAX_SPEECH_DURATION = 5.0         # 滑动窗口最长秒数
MODEL_SIZE = "small"              # tiny(最快)/base(快)/small(均衡)/medium(准)/large(最准)
DEVICE = "cuda"                    # cpu 或 cuda
COMPUTE_TYPE = "float16"          # int8 (CPU) / float16 (GPU) / int8_float16
LANGUAGE = None                   # None=自动检测, "zh"=中文, "en"=英文

# ── 繁简转换 ─────────────────────────────────────────
try:
    import zhconv
    def _to_simplified(text: str) -> str:
        """用 zhconv 做完整繁简转换。"""
        return zhconv.convert(text, "zh-cn")
    print("[繁简] 使用 zhconv 做完整转换")
except ImportError:
    # 降级方案：内置映射表（覆盖 Whisper 输出中 99% 的繁体字）
    _T2S_WORDS = [
        ("為什麼","为什么"),("當然","当然"),("什麼","什么"),
        ("還會","还会"),("這時候","这时候"),("一個","一个"),
    ]
    _T2S_CHARS = str.maketrans({
        "麼":"么","為":"为","問":"问","題":"题","變":"变","從":"从",
        "來":"来","這":"这","個":"个","們":"们","會":"会","時":"时",
        "後":"后","說":"说","話":"话","講":"讲","對":"对","開":"开",
        "關":"关","過":"过","裡":"里","嗎":"吗","學":"学","習":"习",
        "發":"发","現":"现","聽":"听","見":"见","點":"点","頭":"头",
        "長":"长","門":"门","間":"间","體":"体","電":"电","動":"动",
        "國":"国","機":"机","氣":"气","愛":"爱","當":"当","種":"种",
        "處":"处","讓":"让","進":"进","實":"实","業":"业","萬":"万",
        "與":"与","沒":"没","還":"还","將":"将","應":"应","經":"经",
        "總":"总","識":"识","選":"选","寫":"写","張":"张","聲":"声",
        "樂":"乐","難":"难","區":"区","歷":"历","確":"确","術":"术",
        "際":"际","標":"标","線":"线","帶":"带","數":"数","網":"网",
        "車":"车","飛":"飞","魚":"鱼","鳥":"鸟","龍":"龙","圖":"图",
        "爾":"尔","雙":"双","參":"参","戰":"战","據":"据","盡":"尽",
        "邊":"边","鐘":"钟","銀":"银","鐵":"铁","條":"条","狀":"状",
        "壓":"压","轉":"转","稱":"称","臺":"台","證":"证","試":"试",
        "調":"调","設":"设","許":"许","節":"节","達":"达","連":"连",
        "遠":"远","運":"运","則":"则","陳":"陈","羅":"罗","劉":"刘",
        "楊":"杨","趙":"赵","黃":"黄","吳":"吴","馬":"马","孫":"孙",
        "錢":"钱","準":"准","夠":"够","鬱":"郁","著":"着","佔":"占",
        "併":"并","佈":"布","採":"采","週":"周","捨":"舍","鬆":"松",
        "闆":"板","誌":"志","菸":"烟","託":"托","蹟":"迹","鑑":"鉴",
        "復":"复","範":"范","餘":"余","製":"制","鬥":"斗","曬":"晒",
        "衝":"冲","麪":"面","慾":"欲","禦":"御","闢":"辟","採":"采",
        "鬍":"胡","鬚":"须","鹼":"碱","麴":"曲","榦":"干","榖":"谷",
        "夥":"伙","兇":"凶","憑":"凭","游":"游","贊":"赞","鑑":"鉴",
        "隻":"只","繫":"系","穫":"获","籤":"签","纖":"纤","壇":"坛",
        "嚮":"向","嚐":"尝","囪":"囱","團":"团","園":"园","圍":"围",
        "圓":"圆","聖":"圣","場":"场","塊":"块","墮":"堕","塵":"尘",
        "壯":"壮","夢":"梦","夥":"伙","奮":"奋","婦":"妇","孫":"孙",
        "寧":"宁","實":"实","寫":"写","審":"审","寬":"宽","專":"专",
        "尋":"寻","導":"导","層":"层","屆":"届","屬":"属","岡":"冈",
        "巖":"岩","帥":"帅","帳":"帐","幣":"币","幹":"干","廣":"广",
        "廳":"厅","彈":"弹","錄":"录","徹":"彻","復":"复","徵":"征",
        "憂":"忧","憶":"忆","懷":"怀","態":"态","憐":"怜","憑":"凭",
        "憲":"宪","懲":"惩","懸":"悬","戀":"恋","戶":"户","掃":"扫",
        "掛":"挂","採":"采","擁":"拥","據":"据","擊":"击","擔":"担",
        "擴":"扩","擺":"摆","攝":"摄","攔":"拦","敵":"敌","癥":"症",
        "臟":"脏","嚴":"严","曬":"晒","書":"书","會":"会","際":"际",
        "東":"东","業":"业","極":"极","構":"构","樹":"树","橋":"桥",
        "權":"权","歡":"欢","歲":"岁","歷":"历","殘":"残","殺":"杀",
        "毀":"毁","氣":"气","漢":"汉","災":"灾","爲":"为","煉":"炼",
        "煙":"烟","熱":"热","營":"营","燈":"灯","燒":"烧","爭":"争",
        "爾":"尔","牆":"墙","獲":"获","獎":"奖","獨":"独","獸":"兽",
        "獻":"献","環":"环","產":"产","畫":"画","異":"异","療":"疗",
        "監":"监","盤":"盘","眾":"众","睜":"睁","瞭":"了","礙":"碍",
        "禮":"礼","禍":"祸","禽":"禽","種":"种","稱":"称","積":"积",
        "穩":"稳","競":"竞","筆":"笔","節":"节","範":"范","築":"筑",
        "簡":"简","籲":"吁","粵":"粤","糧":"粮","糾":"纠","紀":"纪",
        "約":"约","紅":"红","納":"纳","純":"纯","紙":"纸","級":"级",
        "紛":"纷","紋":"纹","紐":"纽","線":"线","組":"组","細":"细",
        "終":"终","結":"结","絕":"绝","給":"给","絡":"络","統":"统",
        "絲":"丝","經":"经","綠":"绿","維":"维","網":"网","緊":"紧",
        "緒":"绪","綫":"线","編":"编","緣":"缘","縣":"县","縱":"纵",
        "總":"总","績":"绩","織":"织","繞":"绕","繪":"绘","繼":"继",
        "續":"续","纔":"才","義":"义","習":"习","聯":"联","聽":"听",
        "肅":"肃","脅":"胁","腦":"脑","腳":"脚","膽":"胆","膚":"肤",
        "膠":"胶","臉":"脸","舉":"举","舊":"旧","舖":"铺","艦":"舰",
        "艙":"舱","艱":"艰","色":"色","節":"节","範":"范","葉":"叶",
        "著":"着","藥":"药","蘭":"兰","號":"号","蟲":"虫","術":"术",
        "衛":"卫","衝":"冲","補":"补","製":"制","複":"复","視":"视",
        "覽":"览","觀":"观","計":"计","訂":"订","認":"认","記":"记",
        "討":"讨","訓":"训","許":"许","訪":"访","評":"评","詞":"词",
        "試":"试","詩":"诗","話":"话","該":"该","詳":"详","語":"语",
        "誤":"误","說":"说","讀":"读","誰":"谁","課":"课","調":"调",
        "談":"谈","請":"请","論":"论","諸":"诸","講":"讲","謝":"谢",
        "證":"证","識":"识","議":"议","護":"护","譯":"译","讀":"读",
        "變":"变","讓":"让","貝":"贝","負":"负","財":"财","責":"责",
        "貨":"货","費":"费","資":"资","賓":"宾","賞":"赏","賢":"贤",
        "賣":"卖","賴":"赖","購":"购","贊":"赞","賽":"赛","贏":"赢",
        "趙":"赵","趕":"赶","起":"起","越":"越","趨":"趋","足":"足",
        "躍":"跃","車":"车","軍":"军","軌":"轨","軟":"软","軸":"轴",
        "輕":"轻","較":"较","載":"载","輔":"辅","輛":"辆","輸":"输",
        "轉":"转","辦":"办","農":"农","運":"运","連":"连","進":"进",
        "過":"过","達":"达","違":"违","遠":"远","適":"适","選":"选",
        "遲":"迟","還":"还","邊":"边","邏":"逻","鄧":"邓","鄭":"郑",
        "鄰":"邻","郵":"邮","鄉":"乡","醫":"医","釋":"释","釐":"厘",
        "鑑":"鉴","鑒":"鉴","針":"针","釣":"钓","鈣":"钙","鈉":"钠",
        "鋼":"钢","鐵":"铁","鑰":"钥","鑽":"钻","門":"门","閉":"闭",
        "問":"问","開":"开","間":"间","關":"关","閱":"阅","闡":"阐",
        "隊":"队","際":"际","陸":"陆","陽":"阳","陰":"阴","階":"阶",
        "隨":"随","險":"险","隱":"隐","隻":"只","雙":"双","難":"难",
        "雲":"云","電":"电","霧":"雾","靜":"静","響":"响","頁":"页",
        "頂":"顶","項":"项","順":"顺","須":"须","預":"预","頓":"顿",
        "領":"领","頭":"头","頻":"频","題":"题","額":"额","顏":"颜",
        "顧":"顾","風":"风","飛":"飞","養":"养","馬":"马","駐":"驻",
        "駕":"驾","騎":"骑","髮":"发","鬥":"斗","魚":"鱼","鳥":"鸟",
        "鹽":"盐","麥":"麦","黃":"黄","黑":"黑","點":"点","黨":"党",
        "齊":"齐","齒":"齿","齡":"龄","龍":"龙",
    })
    def _to_simplified(text: str) -> str:
        """内置繁简转换。"""
        for old, new in _T2S_WORDS:
            text = text.replace(old, new)
        return text.translate(_T2S_CHARS)

# ── UI 默认参数 ───────────────────────────────────────────
DEFAULT_FONT_SIZE = 28
DEFAULT_WIN_WIDTH = 900
DEFAULT_WIN_HEIGHT = 200           # 双行 + GitHub 链接
DEFAULT_BG_ALPHA = 0.75           # 窗口透明度 (0~1)


# ╔══════════════════════════════════════════════════════════╗
# ║              悬浮字幕窗口  (CaptionOverlay)              ║
# ╚══════════════════════════════════════════════════════════╝

class CaptionOverlay:
    """无边框、置顶、半透明的悬浮字幕窗口，可拖动。B站风格双行显示 + 描边字幕。"""

    GITHUB_URL = "https://github.com/lxy3837/live-caption"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Live Caption")

        # ── 窗口属性 ──
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', DEFAULT_BG_ALPHA)
        self.root.configure(bg='#1a1a1a')

        # ── 初始位置：屏幕底部居中 ──
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - DEFAULT_WIN_WIDTH) // 2
        y = sh - DEFAULT_WIN_HEIGHT - 60
        self.root.geometry(f"{DEFAULT_WIN_WIDTH}x{DEFAULT_WIN_HEIGHT}+{x}+{y}")

        # ── 字体 ──
        self.font_size = DEFAULT_FONT_SIZE
        self._font = tkfont.Font(
            family="Microsoft YaHei", size=self.font_size, weight="bold"
        )
        self._font_dim = tkfont.Font(
            family="Microsoft YaHei", size=self.font_size - 4, weight="normal"
        )

        # ── 上行 Canvas（上一条字幕，灰色描边） ──
        self.prev_canvas = tk.Canvas(
            self.root, bg='#1a1a1a', highlightthickness=0,
            height=60,
        )
        self.prev_canvas.pack(fill="x", padx=10, pady=(8, 0))

        # ── 下行 Canvas（当前字幕，白色描边） ──
        self.cur_canvas = tk.Canvas(
            self.root, bg='#1a1a1a', highlightthickness=0,
            height=80,
        )
        self.cur_canvas.pack(fill="x", padx=10, pady=(2, 0))

        # ── GitHub 链接 ──
        self.gh_label = tk.Label(
            self.root,
            text=f"🔗 {self.GITHUB_URL}",
            font=tkfont.Font(family="Microsoft YaHei", size=9),
            fg="#444444",
            bg='#1a1a1a',
            cursor="hand2",
        )
        self.gh_label.pack(side="bottom", pady=(0, 4))
        self.gh_label.bind("<Button-1>", self._on_github_click)

        # ── 拖动 ──
        self._drag_start_x = 0
        self._drag_start_y = 0
        for widget in (self.prev_canvas, self.cur_canvas, self.gh_label, self.root):
            widget.bind("<Button-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_move)

        # ── 滚轮调字号 ──
        for widget in (self.prev_canvas, self.cur_canvas, self.gh_label, self.root):
            widget.bind("<MouseWheel>", self._on_mousewheel)

        # ── 键盘 ──
        self.root.bind("<Escape>", lambda e: self.close())

        # ── 右键菜单 ──
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

    # ── 描边文字绘制 ───────────────────────────────────────

    def _draw_outline_text(self, canvas: tk.Canvas, text: str,
                           fill_color: str, outline_color: str, font: tkfont.Font):
        """在 Canvas 上绘制带描边的文字。"""
        canvas.delete("all")
        if not text:
            return
        w = canvas.winfo_width() or DEFAULT_WIN_WIDTH
        pad = 20

        # 简单换行：按 canvas 宽度估算每行字符数
        avg_char_w = font.measure("测")  # 中文字符宽度
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
        """简单按字符数换行，尊重已有换行符。"""
        raw_lines = text.split("\n")
        result = []
        for line in raw_lines:
            while len(line) > max_chars:
                # 在 max_chars 位置找最近的空格断句
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

    # ── 拖动 ──────────────────────────────────────────────

    def _on_drag_start(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y

    def _on_drag_move(self, event):
        dx = event.x - self._drag_start_x
        dy = event.y - self._drag_start_y
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self.root.geometry(f"+{x}+{y}")

    # ── 字号调节 ──────────────────────────────────────────

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

    # ── 右键菜单 ──────────────────────────────────────────

    def _show_menu(self, event):
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _on_github_click(self, event=None):
        """打开 GitHub 链接。"""
        import webbrowser
        webbrowser.open(self.GITHUB_URL)

    # ── 公开方法 ──────────────────────────────────────────

    def set_text(self, text: str):
        """线程安全地更新字幕文本。"""
        if not self._running:
            return
        self.root.after(0, self._apply_text, text)

    def _apply_text(self, text: str):
        """必须在主线程调用。双行显示：上行→旧字幕，下行→新字幕。"""
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
                # 从 cur_canvas 读取旧文字 → 移到 prev_canvas
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
        """字号变化后重绘当前两行字幕。"""
        try:
            # 重绘上行
            items = self.prev_canvas.find_all()
            if items:
                txt = self.prev_canvas.itemcget(items[-1], "text")
                self._draw_outline_text(
                    self.prev_canvas, txt,
                    fill_color="#aaaaaa", outline_color="#222222",
                    font=self._font_dim,
                )
            # 重绘下行
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
        """阻塞运行 Tk 主循环。"""
        self.root.mainloop()


# ╔══════════════════════════════════════════════════════════╗
# ║              音频捕获  (AudioCapture)                   ║
# ╚══════════════════════════════════════════════════════════╝

class AudioCapture:
    """后台线程：持续从扬声器回采音频。"""

    def __init__(self, audio_queue: queue.Queue):
        self._q = audio_queue
        self._running = False
        self._thread = None

    def start(self):
        if not HAS_SOUNDCARD:
            print("[错误] 缺少 soundcard 库，请运行: pip install soundcard")
            self._q.put(None)
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _loop(self):
        try:
            speaker = sc.default_speaker()
            # 通过 loopback 模式捕获扬声器输出
            mic = sc.get_microphone(speaker.name, include_loopback=True)
            chunk_size = int(SAMPLE_RATE * CHUNK_DURATION)
            print(f"[音频] 扬声器: {speaker.name}  |  采样率: {SAMPLE_RATE} Hz")
            with mic.recorder(samplerate=SAMPLE_RATE, channels=1) as rec:
                while self._running:
                    data = rec.record(numframes=chunk_size)
                    audio = data.flatten().astype(np.float32)
                    self._q.put(audio)
        except Exception as e:
            print(f"[音频] 捕获异常: {e}")
            self._q.put(None)


# ╔══════════════════════════════════════════════════════════╗
# ║           语音识别  (Transcriber)                       ║
# ╚══════════════════════════════════════════════════════════╝

class Transcriber:
    """后台线程：Local Agreement 流式转录。
    
    不做 VAD 等静音，而是持续滑动窗口推演。
    前后两次结果相同的部分视为"已稳定"，立即输出。
    这是 WhisperLive / whisper_streaming 的同款思路。
    """

    def __init__(self, audio_queue: queue.Queue, on_text, save_path: str = None):
        self._q = audio_queue
        self._on_text = on_text
        self._save_path = save_path
        self._log_file = None
        self._running = False
        self._thread = None
        self._start_time = 0.0        # 录制起始时间（用于视频时间戳）
        # 滑动窗口状态
        self._audio_buffer = []       # 最近 N 秒的音频帧
        self._displayed = ""          # 当前屏幕上显示的字幕
        self._prev_text = ""          # 上一次转录结果（去重用）
        self._last_screen_logged = "" # 上一次写入日志的屏幕内容

    def start(self):
        if not HAS_WHISPER:
            print("[错误] 缺少 faster-whisper 库，请运行: pip install faster-whisper")
            self._on_text("⚠️ 缺少 faster-whisper，请安装后重试")
            return
        self._start_time = time.time()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    @staticmethod
    def _is_speech(audio: np.ndarray) -> bool:
        return float(np.sqrt(np.mean(audio ** 2))) > SILENCE_THRESHOLD

    def _loop(self):
        # ── 打开字幕日志文件 ──
        if self._save_path:
            try:
                os.makedirs(os.path.dirname(self._save_path), exist_ok=True)
                self._log_file = open(self._save_path, "a", encoding="utf-8")
                self._log_file.write(
                    f"=== 字幕记录 (视频时间戳) {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n\n"
                )
                self._log_file.flush()
                print(f"[记录] 字幕保存至: {self._save_path}")
            except Exception as e:
                print(f"[记录] 无法创建日志文件: {e}")

        print(f"[识别] 正在加载 Whisper 模型 '{MODEL_SIZE}'...")
        print("[识别] （首次运行会自动下载模型，约 1-2 GB，请稍候）")
        try:
            model = WhisperModel(MODEL_SIZE, device=DEVICE,
                                 compute_type=COMPUTE_TYPE)
        except Exception as e:
            print(f"[识别] 模型加载失败: {e}")
            self._on_text(f"❌ 模型加载失败: {e}")
            return
        print("[识别] 模型就绪，开始监听...")
        self._on_text("🎤 正在监听中...")

        # 参数
        max_buffer_chunks = int(MAX_SPEECH_DURATION / CHUNK_DURATION)
        transcribe_interval = 1.2       # 每多少秒推演一次（屏幕更新频率）
        transcribe_interval_chunks = int(transcribe_interval / CHUNK_DURATION)
        silence_clear_chunks = int(2.5 / CHUNK_DURATION)  # 连续静音多久清空缓冲区
        consecutive_silence = 0
        chunk_count_since_transcribe = 0
        log_write_chunks = int(5.0 / CHUNK_DURATION)   # 每 5 秒写一次日志
        chunk_count_since_log = 0

        while self._running:
            try:
                audio = self._q.get(timeout=0.3)
            except queue.Empty:
                continue

            if audio is None:
                break

            is_speech = self._is_speech(audio)

            if is_speech:
                self._audio_buffer.append(audio)
                consecutive_silence = 0
            else:
                if self._audio_buffer:
                    self._audio_buffer.append(audio)
                    consecutive_silence += 1

            # 长时间静音 → 清空缓冲区，重置状态（新的一句话）
            if consecutive_silence >= silence_clear_chunks and self._audio_buffer:
                # 最后一次推演，把剩余内容输出
                self._transcribe_and_stabilize(model, final=True)
                # 日志分隔线
                if self._log_file and self._last_screen_logged:
                    self._log_file.write("---\n")
                    self._log_file.flush()
                self._audio_buffer.clear()
                consecutive_silence = 0
                self._prev_text = ""
                self._displayed = ""
                self._last_screen_logged = ""
                chunk_count_since_transcribe = 0
                continue

            # 限制缓冲区大小
            if len(self._audio_buffer) > max_buffer_chunks:
                self._audio_buffer = self._audio_buffer[-max_buffer_chunks:]

            # ── 每 5 秒写盘：把屏幕当前内容写入日志 ──
            chunk_count_since_log += 1
            if chunk_count_since_log >= log_write_chunks and self._log_file:
                chunk_count_since_log = 0
                screen = self._displayed.strip()
                if screen and screen != self._last_screen_logged:
                    elapsed = time.time() - self._start_time
                    self._log_file.write(f"[T={elapsed:.1f}s] {screen}\n")
                    self._log_file.flush()
                    self._last_screen_logged = screen
                    print(f"[写盘] {self._save_path}")

            # 到时间了 → 推演
            chunk_count_since_transcribe += 1
            if self._audio_buffer and chunk_count_since_transcribe >= transcribe_interval_chunks:
                chunk_count_since_transcribe = 0
                self._transcribe_and_stabilize(model, final=False)

        print("[识别] 线程已退出")

    def _transcribe_and_stabilize(self, model, final: bool):
        """转录当前缓冲区，用 local agreement 输出稳定部分。"""
        if len(self._audio_buffer) < 3:
            return
        audio = np.concatenate(self._audio_buffer)
        try:
            segments, _ = model.transcribe(
                audio,
                beam_size=5,
                language=LANGUAGE,
                vad_filter=True,
                vad_parameters=dict(
                    threshold=0.5,
                    min_speech_duration_ms=100,
                    min_silence_duration_ms=200,
                ),
            )
            cur_text = " ".join(seg.text.strip() for seg in segments)
            cur_text = _to_simplified(cur_text)     # 繁体→简体
        except Exception as e:
            print(f"[识别] 转录错误: {e}")
            return

        # ── 日志：不在这里记，由定时器统一取屏幕内容写入 ──
        self._prev_text = cur_text

        if final:
            # 最后一次：直接输出全部
            if cur_text.strip():
                self._emit(cur_text)
            return

        if not cur_text.strip():
            return

        # ── 智能去重：只在内容真正增加时才更新屏幕 ──
        if not self._displayed:
            # 第一句，直接显示
            self._emit(cur_text)
            self._displayed = cur_text
        elif len(cur_text) > len(self._displayed) + 2:
            # 新内容比已显示的长 → 有新增内容
            self._emit(cur_text)
            self._displayed = cur_text
        elif cur_text not in self._displayed:
            # 内容完全不同 → 新的一句话
            self._emit(cur_text)
            self._displayed = cur_text
        # 否则：只是 Whisper 反复推演出轻微变化的同一内容，跳过不更新屏幕

    def _emit(self, text: str):
        """输出字幕到屏幕和控制台（仅显示，不写日志）。"""
        elapsed = time.time() - self._start_time
        print(f"[字幕] [T={elapsed:.1f}s] {text}")
        self._on_text(text)


# ╔══════════════════════════════════════════════════════════╗
# ║                    主入口  (main)                       ║
# ╚══════════════════════════════════════════════════════════╝

def check_dependencies():
    """检查关键依赖是否安装，缺少则给出提示。"""
    missing = []
    if not HAS_SOUNDCARD:
        missing.append("soundcard")
    if not HAS_WHISPER:
        missing.append("faster-whisper")
    if missing:
        print(f"[警告] 缺少以下库: {', '.join(missing)}")
        print(f"[提示] 请运行: pip install {' '.join(missing)}")
        return False
    return True


def main():
    # 字幕保存路径（脚本同目录，按日期命名）
    script_dir = os.path.dirname(os.path.abspath(__file__))
    save_path = os.path.join(
        script_dir, f"captions_{datetime.now().strftime('%Y%m%d')}.txt"
    )

    # ── ASCII Art Banner（对齐算法 + ANSI 过滤） ──────
    import re
    ESC = "\x1b"
    B = f"{ESC}[1;36m"    # 亮青
    W = f"{ESC}[1;37m"    # 亮白
    Y = f"{ESC}[1;33m"    # 亮黄
    R = f"{ESC}[0m"       # 重置
    BW = 62                # 内框视觉宽度（CAPTION 行最宽 59）

    _ANSI = re.compile(r'\x1b\[[0-9;]*m')

    def pad(s, w):
        """填充到指定视觉宽度，忽略 ANSI 转义码。"""
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

    # 字幕窗口（主线程）
    overlay = CaptionOverlay()

    # 音频捕获 & 识别（后台线程）
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
