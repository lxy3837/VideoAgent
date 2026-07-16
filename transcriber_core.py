"""
转录核心模块 — 音频采集 + Whisper 识别（纯逻辑，无 GUI）

从 live_caption_video.py 拆分出来，供 MCP Server 和 GUI 共用。
"""

import threading
import queue
import time
import sys
import os
import warnings
from datetime import datetime
import numpy as np

# ── 抑制 soundcard 录制抖动警告 ──
import soundcard.mediafoundation as _sc_mf
warnings.filterwarnings("ignore", category=_sc_mf.SoundcardRuntimeWarning)

# ── 模型下载目录 ──
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ["HF_HOME"] = os.path.join(_SCRIPT_DIR, "models")
os.environ["HUGGINGFACE_HUB_CACHE"] = os.environ["HF_HOME"]
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# ── 音频捕获 ──
try:
    import soundcard as sc
    HAS_SOUNDCARD = True
except ImportError:
    HAS_SOUNDCARD = False

# ── 语音识别 ──
try:
    from faster_whisper import WhisperModel
    HAS_WHISPER = True
except ImportError:
    HAS_WHISPER = False

# ── 配置 ──
SAMPLE_RATE = 16000
CHUNK_DURATION = 0.5
SILENCE_THRESHOLD = 0.008
MAX_SPEECH_DURATION = 5.0
MODEL_SIZE = "small"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"
LANGUAGE = None

# ── 繁简转换 ──
try:
    import zhconv
    def _to_simplified(text: str) -> str:
        return zhconv.convert(text, "zh-cn")
except ImportError:
    _T2S_CHARS = str.maketrans({
        "麼":"么","為":"为","麼":"么","問":"问","題":"题","變":"变","從":"从",
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
        "衝":"冲","麪":"面","慾":"欲","禦":"御","闢":"辟",
        "鬍":"胡","鬚":"须","鹼":"碱","麴":"曲","榦":"干","榖":"谷",
        "夥":"伙","兇":"凶","憑":"凭","游":"游","贊":"赞",
        "隻":"只","繫":"系","穫":"获","籤":"签","纖":"纤","壇":"坛",
        "嚮":"向","嚐":"尝","囪":"囱","團":"团","園":"园","圍":"围",
        "圓":"圆","聖":"圣","場":"场","塊":"块","墮":"堕","塵":"尘",
        "壯":"壮","夢":"梦","奮":"奋","婦":"妇",
        "寧":"宁","寫":"写","審":"审","寬":"宽","專":"专",
        "尋":"寻","導":"导","層":"层","屆":"届","屬":"属","岡":"冈",
        "巖":"岩","帥":"帅","帳":"帐","幣":"币","幹":"干","廣":"广",
        "廳":"厅","彈":"弹","錄":"录","徹":"彻","徵":"征",
        "憂":"忧","憶":"忆","懷":"怀","態":"态","憐":"怜",
        "憲":"宪","懲":"惩","懸":"悬","戀":"恋","戶":"户","掃":"扫",
        "掛":"挂","擁":"拥","據":"据","擊":"击","擔":"担",
        "擴":"扩","擺":"摆","攝":"摄","攔":"拦","敵":"敌","癥":"症",
        "臟":"脏","嚴":"严","書":"书",
        "東":"东","極":"极","構":"构","樹":"树","橋":"桥",
        "權":"权","歡":"欢","歲":"岁","歷":"历","殘":"残","殺":"杀",
        "毀":"毁","氣":"气","漢":"汉","災":"灾","爲":"为","煉":"炼",
        "煙":"烟","熱":"热","營":"营","燈":"灯","燒":"烧","爭":"争",
        "牆":"墙","獲":"获","獎":"奖","獨":"独","獸":"兽",
        "獻":"献","環":"环","產":"产","畫":"画","異":"异","療":"疗",
        "監":"监","盤":"盘","眾":"众","睜":"睁","瞭":"了","礙":"碍",
        "禮":"礼","禍":"祸","禽":"禽","稱":"称","積":"积",
        "穩":"稳","競":"竞","筆":"笔","節":"节","築":"筑",
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
        "艙":"舱","艱":"艰","色":"色","葉":"叶",
        "著":"着","藥":"药","蘭":"兰","號":"号","蟲":"虫","術":"术",
        "衛":"卫","衝":"冲","補":"补","製":"制","複":"复","視":"视",
        "覽":"览","觀":"观","計":"计","訂":"订","認":"认","記":"记",
        "討":"讨","訓":"训","許":"许","訪":"访","評":"评","詞":"词",
        "試":"试","詩":"诗","話":"话","該":"该","詳":"详","語":"语",
        "誤":"误","說":"说","讀":"读","誰":"谁","課":"课","調":"调",
        "談":"谈","請":"请","論":"论","諸":"诸","講":"讲","謝":"谢",
        "證":"证","識":"识","議":"议","護":"护","譯":"译",
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
        return text.translate(_T2S_CHARS)


def check_dependencies():
    """检查关键依赖是否安装。"""
    missing = []
    if not HAS_SOUNDCARD:
        missing.append("soundcard")
    if not HAS_WHISPER:
        missing.append("faster-whisper")
    if missing:
        print(f"[警告] 缺少: {', '.join(missing)}")
        print(f"[提示] pip install {' '.join(missing)}")
        return False
    return True


# ═══════════════════════════════════════════════════════════
#  AudioCapture
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
#  Transcriber
# ═══════════════════════════════════════════════════════════

class Transcriber:
    """后台线程：Local Agreement 流式转录。"""

    def __init__(self, audio_queue: queue.Queue, on_text, save_path: str = None):
        self._q = audio_queue
        self._on_text = on_text
        self._save_path = save_path
        self._log_file = None
        self._running = False
        self._thread = None
        self._start_time = 0.0
        self._audio_buffer = []
        self._displayed = ""
        self._prev_text = ""
        self._last_screen_logged = ""

    def start(self):
        if not HAS_WHISPER:
            print("[错误] 缺少 faster-whisper 库")
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

    @property
    def displayed(self) -> str:
        return self._displayed

    @property
    def start_time(self) -> float:
        return self._start_time

    @property
    def log_path(self) -> str:
        return self._save_path or ""

    @staticmethod
    def _is_speech(audio: np.ndarray) -> bool:
        return float(np.sqrt(np.mean(audio ** 2))) > SILENCE_THRESHOLD

    def _loop(self):
        if self._save_path:
            try:
                os.makedirs(os.path.dirname(self._save_path), exist_ok=True)
                self._log_file = open(self._save_path, "a", encoding="utf-8")
                self._log_file.write(
                    f"=== 字幕记录 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n\n"
                )
                self._log_file.flush()
                print(f"[记录] 字幕保存至: {self._save_path}")
            except Exception as e:
                print(f"[记录] 无法创建日志文件: {e}")

        print(f"[识别] 正在加载 Whisper 模型 '{MODEL_SIZE}'...")
        try:
            model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        except Exception as e:
            print(f"[识别] 模型加载失败: {e}")
            self._on_text(f"❌ 模型加载失败: {e}")
            return
        print("[识别] 模型就绪，开始监听...")
        self._on_text("🎤 正在监听中...")

        max_buffer_chunks = int(MAX_SPEECH_DURATION / CHUNK_DURATION)
        transcribe_interval = 1.2
        transcribe_interval_chunks = int(transcribe_interval / CHUNK_DURATION)
        silence_clear_chunks = int(2.5 / CHUNK_DURATION)
        consecutive_silence = 0
        chunk_count_since_transcribe = 0
        log_write_chunks = int(5.0 / CHUNK_DURATION)
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

            if consecutive_silence >= silence_clear_chunks and self._audio_buffer:
                self._transcribe_and_stabilize(model, final=True)
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

            if len(self._audio_buffer) > max_buffer_chunks:
                self._audio_buffer = self._audio_buffer[-max_buffer_chunks:]

            chunk_count_since_log += 1
            if chunk_count_since_log >= log_write_chunks and self._log_file:
                chunk_count_since_log = 0
                screen = self._displayed.strip()
                if screen and screen != self._last_screen_logged:
                    elapsed = time.time() - self._start_time
                    self._log_file.write(f"[T={elapsed:.1f}s] {screen}\n")
                    self._log_file.flush()
                    self._last_screen_logged = screen

            chunk_count_since_transcribe += 1
            if self._audio_buffer and chunk_count_since_transcribe >= transcribe_interval_chunks:
                chunk_count_since_transcribe = 0
                self._transcribe_and_stabilize(model, final=False)

        print("[识别] 线程已退出")

    def _transcribe_and_stabilize(self, model, final: bool):
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
            cur_text = _to_simplified(cur_text)
        except Exception as e:
            print(f"[识别] 转录错误: {e}")
            return

        self._prev_text = cur_text

        if final:
            if cur_text.strip():
                self._emit(cur_text)
            return

        if not cur_text.strip():
            return

        if not self._displayed:
            self._emit(cur_text)
            self._displayed = cur_text
        elif len(cur_text) > len(self._displayed) + 2:
            self._emit(cur_text)
            self._displayed = cur_text
        elif cur_text not in self._displayed:
            self._emit(cur_text)
            self._displayed = cur_text

    def _emit(self, text: str):
        elapsed = time.time() - self._start_time
        print(f"[字幕] [T={elapsed:.1f}s] {text}")
        self._on_text(text)
