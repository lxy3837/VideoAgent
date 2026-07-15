"""
多模态视频理解 Agent v3 — 时间戳锚点 + LLM预测 + seek对齐
=============================================================

架构:
  LLM (大脑) → 预测目标时间段
    → video_capture_sequence (粗扫描)
    → LLM 视觉判断，锁定精确时间
    → seek(target) → play N秒 → pause → 等whisper追上
    → OCR + 字幕 按 video.currentTime 对齐，延迟靠暂停消除

核心哲学:
  - 所有数据统一用 video.currentTime 当锚点
  - whisper 的 1.x 秒延迟靠 Agent 暂停视频来消除（视频停了，延迟无所谓）
  - LLM 做预测，Agent 用 seek 验证
"""

import asyncio
import json
import subprocess
import sys
import os
import time
import threading
import queue
from dataclasses import dataclass, field
from typing import Optional, Callable

SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "index.js")


# ═══════════════════════════════════════════════════════════
#  MCP 轻量客户端
# ═══════════════════════════════════════════════════════════

class McpClient:
    """通过 stdio 与 MCP Server 通信。"""

    def __init__(self, server_path: str):
        self.server_path = server_path
        self.process: Optional[subprocess.Popen] = None
        self._req_id = 0

    async def start(self):
        self.process = subprocess.Popen(
            ["node", self.server_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        await self._send({
            "jsonrpc": "2.0", "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "video-agent", "version": "3.0.0"}},
            "id": self._next_id(),
        })
        resp = await self._recv()
        if "error" in resp:
            raise RuntimeError(f"MCP init failed: {resp['error']}")
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def call_tool(self, name: str, arguments: dict) -> dict:
        await self._send({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": self._next_id(),
        })
        return await self._recv()

    async def close(self):
        if self.process:
            self.process.stdin.close()
            self.process.terminate()
            await asyncio.sleep(0.5)
            if self.process.poll() is None:
                self.process.kill()

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _send(self, msg: dict):
        data = json.dumps(msg) + "\n"
        self.process.stdin.write(data.encode())
        self.process.stdin.flush()

    async def _recv(self) -> dict:
        if not self.process or not self.process.stdout:
            raise RuntimeError("Client closed")
        line = await asyncio.get_event_loop().run_in_executor(None, self.process.stdout.readline)
        if not line:
            raise RuntimeError("Server closed")
        return json.loads(line.decode())


# ═══════════════════════════════════════════════════════════
#  时间戳锚点 — 所有数据的统一标签
# ═══════════════════════════════════════════════════════════

@dataclass
class TimestampAnchor:
    """
    一切数据的锚点。
    video_time = 数据采集瞬间的 video.currentTime
    wall_time  = 真实世界时间（仅调试）
    """
    video_time: float
    wall_time: float = field(default_factory=time.time)

    @staticmethod
    def now() -> "TimestampAnchor":
        return TimestampAnchor(video_time=-1, wall_time=time.time())

    def __repr__(self):
        return f"T_video={self.video_time:.2f}s"


@dataclass
class VideoFrame:
    """
    时间戳对齐的一帧多模态数据。

    caption_offset: whisper 字幕的预估延迟（秒）。
                   正数 = 字幕晚于实际音频。
                   暂停可消除此偏移（视频停了，字幕都是刚才那段）。
    """
    anchor: TimestampAnchor
    screenshot_path: str = ""
    ocr_text: Optional[str] = None
    caption: Optional[str] = None
    caption_offset: float = 0.0      # whisper 延迟估计
    ready_state: int = 0

    def is_aligned(self, tolerance: float = 0.3) -> bool:
        """OCR 和字幕的时间是否对齐（延迟可忽略）。"""
        return self.caption_offset < tolerance

    def summary(self) -> str:
        parts = [f"@{self.anchor}"]
        if self.ocr_text:
            parts.append(f"OCR: {self.ocr_text[:80]}")
        if self.caption:
            offset_str = f" +{self.caption_offset:.1f}s" if self.caption_offset > 0.1 else ""
            parts.append(f"字幕{offset_str}: {self.caption[:80]}")
        return " | ".join(parts)


# ═══════════════════════════════════════════════════════════
#  数据采集器 — 截帧 + 时间戳
# ═══════════════════════════════════════════════════════════

class FrameCollector:
    """
    统一采集视频帧的时间戳数据。

    核心方法:
      capture_now()      — 截当前帧 + 打时间戳
      capture_at(t)      — seek(t) → 等就绪 → 截帧 + 打时间戳
      capture_sequence() — 区间等间隔采样
    """

    def __init__(self, client: McpClient):
        self.client = client

    async def capture_now(self) -> VideoFrame:
        """截取当前画面，返回带时间戳的帧。"""
        # 同时获取状态和截图
        state_resp = await self.client.call_tool("video_get_state", {})
        scr_resp = await self.client.call_tool("video_screenshot", {})

        wall_time = time.time()

        # 解析 video.currentTime
        video_time = 0.0
        state_content = state_resp.get("result", {}).get("content", [])
        for block in state_content:
            if block.get("type") == "text":
                state = json.loads(block["text"])
                if state.get("hasVideo"):
                    video_time = state["videos"][0].get("currentTime", 0)

        # 解析截图元数据
        filepath = ""
        for block in scr_resp.get("result", {}).get("content", []):
            if block.get("type") == "text":
                meta = json.loads(block["text"])
                filepath = meta.get("filepath", "")

        return VideoFrame(
            anchor=TimestampAnchor(video_time=video_time, wall_time=wall_time),
            screenshot_path=filepath,
            ready_state=state.get("videos", [{}])[0].get("readyState", 0) if state.get("hasVideo") else 0,
        )

    async def capture_at(self, seconds: float, play_duration: float = 0) -> VideoFrame:
        """
        seek 到指定时间，可选播放一段，然后捕获。

        如果 play_duration > 0:
          seek(seconds) → play → 等 play_duration → pause → 等 2s 给 whisper 追上 → 截图
        这就是你说的"暂停对齐"策略。
        """
        if play_duration > 0:
            # seek + play + pause 流程
            await self.client.call_tool("video_play", {})
            resp = await self.client.call_tool("video_capture_at", {"seconds": seconds})

            # 播放 play_duration 秒
            await asyncio.sleep(play_duration)

            # 暂停 — 此时视频停在目标 + play_duration 的位置
            await self.client.call_tool("video_pause", {})

            # 等待 whisper 延迟追上（视频已暂停，whisper 输出的是刚才播放的内容）
            whisper_wait = 2.5  # 给 whisper 足够的追赶时间
            await asyncio.sleep(whisper_wait)

            # 现在截图 — 视频停在精确位置，whisper 也跟上了
            frame = await self.capture_now()
            frame.caption_offset = 0.0  # 暂停策略消除了偏移
            return frame
        else:
            # 仅 seek → 截图
            resp = await self.client.call_tool("video_capture_at", {"seconds": seconds})
            content = resp.get("result", {}).get("content", [])
            for block in content:
                if block.get("type") == "text":
                    meta = json.loads(block["text"])
                    return VideoFrame(
                        anchor=TimestampAnchor(video_time=meta.get("actualTime", seconds)),
                        screenshot_path=meta.get("filepath", ""),
                        ready_state=meta.get("readyState", 0),
                    )
            return VideoFrame(anchor=TimestampAnchor(video_time=seconds))

    async def capture_sequence(
        self, start: float, end: float, interval: float, max_frames: int = 20
    ) -> list[VideoFrame]:
        """粗扫描：在 [start, end] 内等间隔采样。"""
        frames: list[VideoFrame] = []
        t = start
        count = 0
        while t <= end and count < max_frames:
            frame = await self.capture_at(t)
            frames.append(frame)
            t += interval
            count += 1
            print(f"  [扫描] {count}/{max_frames} @ T_video={frame.anchor.video_time:.1f}s")
        return frames


# ═══════════════════════════════════════════════════════════
#  Whisper 字幕桥接（对接 live_caption.py）
# ═══════════════════════════════════════════════════════════

class CaptionBridge:
    """
    对接 G:\\本地字幕\\live_caption.py 的输出。

    策略:
      - live_caption.py 作为独立进程运行，输出到 captions_YYYYMMDD.txt
      - 本类轮询日志文件，读取最新字幕行
      - 每条字幕格式: [HH:MM:SS] 字幕文本
      - 转成带 video_time 锚点的数据结构

    实际使用时要启动 live_caption.py 进程（或手动运行），
    本类只负责读取。
    """

    def __init__(self, caption_log_path: str):
        self.log_path = caption_log_path
        self._last_pos = 0
        self._lock = threading.Lock()
        self._caption_queue: queue.Queue = queue.Queue()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # 对齐基准：记录 (wall_time, video_time) 作为时间映射
        self._t0_wall: float = 0.0
        self._t0_video: float = 0.0
        self._calibrated = False

    def calibrate(self, video_time: float):
        """
        校准：告诉桥接器 "此刻 wall_time 对应的 video_time 是多少"。

        每次暂停后调用一次，确保 wall_time → video_time 的映射准确。
        """
        self._t0_wall = time.time()
        self._t0_video = video_time
        self._calibrated = True

    def wall_to_video(self, wall_t: float) -> float:
        """把真实世界时间戳转换为估算的视频时间戳。"""
        if not self._calibrated:
            return -1
        return self._t0_video + (wall_t - self._t0_wall)

    def start_polling(self):
        """启动后台线程轮询字幕文件。"""
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def get_latest_caption(self) -> Optional[tuple[str, float]]:
        """非阻塞获取最新字幕（文本, 视频时间戳估计）。"""
        try:
            return self._caption_queue.get_nowait()
        except queue.Empty:
            return None

    def _poll_loop(self):
        """轮询 captions log 文件。"""
        import re
        if not os.path.exists(self.log_path):
            print(f"[CaptionBridge] 日志文件不存在: {self.log_path}")
            return

        while self._running:
            try:
                with open(self.log_path, "r", encoding="utf-8") as f:
                    f.seek(self._last_pos)
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("===") or line == "---":
                            continue
                        # 匹配 [HH:MM:SS] 格式
                        m = re.match(r"\[(\d{2}):(\d{2}):(\d{2})\]\s+(.+)", line)
                        if m:
                            h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                            wall_t = h * 3600 + mi * 60 + s  # 当天的时间
                            text = m.group(4)
                            video_t = self.wall_to_video(wall_t)
                            self._caption_queue.put((text, video_t))
                    self._last_pos = f.tell()
            except Exception as e:
                print(f"[CaptionBridge] 读取异常: {e}")
            time.sleep(1.0)  # 每秒轮询一次


# ═══════════════════════════════════════════════════════════
#  Agent — LLM 驱动的多模态视频理解循环
# ═══════════════════════════════════════════════════════════

class VideoAgent:
    """
    多模态视频理解 Agent。

    决策循环:
      1. LLM 预测目标时间段
      2. collector.capture_sequence() 粗扫描
      3. LLM 视觉判断，锁定精确时间
      4. seek → play → pause → 等whisper → 精确捕获
      5. 下一个问题 / 结束

    whisper 延迟对策:
      每次 "play N秒 → pause → 等 2.5s" 后，
      whisper 输出已经追上，caption_offset ≈ 0。
    """

    def __init__(self, client: McpClient, caption_bridge: Optional[CaptionBridge] = None):
        self.client = client
        self.collector = FrameCollector(client)
        self.caption_bridge = caption_bridge
        self.history: list[VideoFrame] = []  # 已采集的所有帧

    async def navigate(self, url: str) -> dict:
        """打开视频页面。"""
        resp = await self.client.call_tool("video_navigate", {
            "url": url, "waitUntil": "domcontentloaded",
        })
        content = resp.get("result", {}).get("content", [])
        for block in content:
            if block.get("type") == "text":
                return json.loads(block["text"])
        return {}

    async def get_video_info(self) -> dict:
        """获取视频基本信息（时长等）。"""
        resp = await self.client.call_tool("video_get_state", {})
        for block in resp.get("result", {}).get("content", []):
            if block.get("type") == "text":
                return json.loads(block["text"])
        return {}

    async def coarse_scan(self, start: float, end: float, interval: float = 60) -> list[VideoFrame]:
        """
        粗扫描: 在 [start, end] 内每隔 interval 秒截一帧。
        返回帧列表供 LLM 视觉分析，缩小搜索范围。
        """
        frames = await self.collector.capture_sequence(start, end, interval)
        self.history.extend(frames)
        return frames

    async def fine_capture(self, target: float, listen_duration: float = 8.0) -> VideoFrame:
        """
        精确捕获: seek → play → pause → 等whisper → 截帧。

        这是"暂停对齐"策略的核心:
          - seek 到 target
          - 播放 listen_duration 秒（让 whisper 有内容可转录）
          - 暂停
          - 等 2.5s 让 whisper 追上
          - 截帧 + 读字幕
          - whisper 延迟被暂停消除了
        """
        await self.client.call_tool("video_play", {})

        # seek 到目标时间
        seek_resp = await self.client.call_tool("video_seek", {
            "seconds": target, "tolerance": 0.3, "maxWaitMs": 8000,
        })

        # 播放
        await asyncio.sleep(listen_duration)

        # 暂停 — 关键步骤
        await self.client.call_tool("video_pause", {})

        # 等 whisper 追上
        print(f"  [Agent] 暂停，等待 whisper 延迟追上...")
        await asyncio.sleep(2.5)

        # 校准时间戳桥
        state = await self.get_video_info()
        if self.caption_bridge and state.get("hasVideo"):
            self.caption_bridge.calibrate(state["videos"][0]["currentTime"])

        # 截帧
        frame = await self.collector.capture_now()

        # 读取最新字幕
        if self.caption_bridge:
            latest = self.caption_bridge.get_latest_caption()
            while self.caption_bridge.get_latest_caption():
                latest = self.caption_bridge.get_latest_caption()  # 跳到最新
            if latest:
                frame.caption = latest[0]
                frame.caption_offset = abs(frame.anchor.video_time - latest[1]) if latest[1] > 0 else 0

        self.history.append(frame)
        print(f"  [Agent] 精确捕获: {frame.summary()}")
        return frame

    async def answer_question(self, question: str, video_duration: float) -> list[VideoFrame]:
        """
        Agent 主循环 —— 回答一个关于视频的问题。

        这是你要自己设计的地方。以下是一套示范流程:
          coarse_scan(duration*0.1, duration*0.9, interval=duration/10)
          → LLM 看截图找目标
          → fine_capture(锁定位置)
          → 返回结果
        """
        print(f"\n{'='*60}")
        print(f"  Agent 任务: {question}")
        print(f"  视频时长: {video_duration:.0f}s")
        print(f"{'='*60}")

        # Step 1: 粗扫描 — 均匀采样
        print("\n[Step 1] 粗扫描...")
        interval = max(30, video_duration / 10)
        frames = await self.coarse_scan(
            video_duration * 0.05,
            video_duration * 0.95,
            interval,
        )
        print(f"  粗扫描完成: {len(frames)} 帧")

        # Step 2: 交给 LLM 分析（此处是占位 — 实际要调 LLM API）
        # LLM 收到 10 张截图后判断目标在哪个区间
        # 返回值是"预测的目标时间"
        predicted_time = await self._llm_predict(frames, question)

        if predicted_time is None:
            print("[Agent] LLM 无法定位目标")
            return frames

        # Step 3: 精确捕获
        print(f"\n[Step 2] 精确捕获 @ {predicted_time:.0f}s...")
        fine_frame = await self.fine_capture(predicted_time, listen_duration=8.0)

        return [fine_frame]

    async def _llm_predict(self, frames: list[VideoFrame], question: str) -> Optional[float]:
        """
        LLM 预测 — 给 LLM 看多帧截图，让它判断目标时间。

        这是你要接入 LLM API 的地方。
        示范逻辑（无 LLM 时返回中间帧时间）:
        """
        if not frames:
            return None

        # TODO: 替换为实际 LLM 调用
        # prompt = f"这些是视频截图，每帧标注了时间。请找出{question}出现的时间。"
        # response = llm.chat(prompt, images=[f.screenshot_path for f in frames])
        # return parse_time(response)

        # 占位: 返回中间帧
        mid = frames[len(frames) // 2]
        return mid.anchor.video_time

    async def close(self):
        await self.client.close()


# ═══════════════════════════════════════════════════════════
#  演示
# ═══════════════════════════════════════════════════════════

async def demo():
    print("=" * 60)
    print("  多模态视频理解 Agent v3")
    print("  核心: video.currentTime 锚点 + 暂停消除whisper延迟")
    print("=" * 60)

    client = McpClient(SERVER_PATH)
    await client.start()

    agent = VideoAgent(client)

    try:
        # 演示 1: 粗扫描
        print("\n--- 演示 1: 粗扫描 ---")
        print("对视频 5%-95% 区间等间隔采样 10 帧，供 LLM 视觉定位")
        print("调用: agent.coarse_scan(duration*0.05, duration*0.95, interval=duration/10)")
        print("LLM 收到 10 张带时间戳的截图 → 预测目标在哪")

        # 演示 2: 精确捕获 + 暂停对齐
        print("\n--- 演示 2: 精确捕获 + 暂停对齐 ---")
        print("流程:")
        print("  1. seek(target)")
        print("  2. play(8s)       ← 给 whisper 喂音频")
        print("  3. pause()         ← 视频停止")
        print("  4. sleep(2.5s)     ← 等 whisper 延迟追上")
        print("  5. capture_now()   ← 截帧+字幕，video.currentTime 对齐")
        print()
        print("此时 whisper 延迟已消除:")
        print("  视频停在 T=428.3s")
        print("  whisper 输出: '...Q K V 分别代表查询、键、值...'")
        print("  OCR 结果: 画面显示 'QKV 计算公式'")
        print("  → caption_offset ≈ 0, 完全对齐")

        # 演示 3: 完整流程
        print("\n--- 演示 3: 完整问答流程 ---")
        print("await agent.answer_question('视频中哪里讲了梯度下降?', duration=900)")
        print()
        print("内部流程:")
        print("  [粗扫描] capture_sequence(45, 855, interval=90s) → 10帧")
        print("  [LLM判断] '第5帧(405s)有梯度下降标题'")
        print("  [精确捕获] fine_capture(405, listen_duration=8s)")
        print("    → seek(405) → play 8s → pause → 等2.5s → 截帧+字幕")
        print("  [返回] 对齐后的帧，OCR+字幕都指向同一时间")

        print("\n" + "=" * 60)
        print("以上。Agent 循环框架已就绪。")
        print("你需要替换的部分:")
        print("  1. _llm_predict() — 接入 LLM API 做视觉判断")
        print("  2. CaptionBridge — 对接 live_caption.py 的输出")
        print("  3. answer_question() — 设计你自己的决策逻辑")
        print("=" * 60)

    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(demo())
