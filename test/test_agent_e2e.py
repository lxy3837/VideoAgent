"""
Agent 端到端测试 — 我就是 Agent，跑通完整链路
=================================================
眼睛: easyocr（简单模型，本地跑）
耳朵: live_caption.py（Whisper 实时转录）
大脑: 我（人肉 LLM）读字幕文本 → 判断哪里截图 → seek → pause → 截图 → OCR

统一度量衡: video.currentTime

流程:
  1. 启动 MCP Server
  2. 导航到视频
  3. 获取视频时长
  4. 粗扫描：在几个关键时间点截图 + OCR
  5. 验证: 时间戳 → seek → 截图 → OCR 全链路
"""
import asyncio
import json
import os
import sys
import subprocess
import time
from datetime import datetime

SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "index.js")

# ═══════════════════════════════════════════════════════════
#  MCP 轻量客户端 (直接从 agent.py 移植)
# ═══════════════════════════════════════════════════════════

class McpClient:
    def __init__(self, server_path: str):
        self.server_path = server_path
        self.process = None
        self._req_id = 0

    async def start(self):
        self.process = subprocess.Popen(
            ["node", self.server_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        await self._send({
            "jsonrpc": "2.0", "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "video-agent", "version": "4.0.0"}},
            "id": self._next_id(),
        })
        resp = await self._recv()
        if "error" in resp:
            raise RuntimeError(f"MCP init failed: {resp['error']}")
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        print("[MCP] Server 已连接")

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
#  Agent 工具箱
# ═══════════════════════════════════════════════════════════

class AgentToolkit:
    """Agent 的手和眼 — 封装所有 MCP 调用。"""

    def __init__(self, client: McpClient):
        self.client = client

    async def navigate(self, url: str) -> dict:
        resp = await self.client.call_tool("video_navigate", {"url": url})
        return self._parse_text(resp)

    async def get_state(self) -> dict:
        resp = await self.client.call_tool("video_get_state", {})
        return self._parse_text(resp)

    async def play(self) -> dict:
        resp = await self.client.call_tool("video_play", {})
        return self._parse_text(resp)

    async def pause(self) -> dict:
        resp = await self.client.call_tool("video_pause", {})
        return self._parse_text(resp)

    async def seek(self, seconds: float) -> dict:
        resp = await self.client.call_tool("video_seek", {
            "seconds": seconds, "tolerance": 0.5, "maxWaitMs": 8000,
        })
        return self._parse_text(resp)

    async def screenshot(self) -> tuple[str, dict]:
        """截图，返回 (文件路径, 元数据)。"""
        resp = await self.client.call_tool("video_screenshot", {})
        meta = self._parse_text(resp)
        return meta.get("filepath", ""), meta

    async def capture_at(self, seconds: float) -> tuple[str, dict]:
        """在指定时间截帧。"""
        resp = await self.client.call_tool("video_capture_at", {"seconds": seconds})
        meta = self._parse_text(resp)
        return meta.get("filepath", ""), meta

    @staticmethod
    def _parse_text(resp: dict) -> dict:
        for block in resp.get("result", {}).get("content", []):
            if block.get("type") == "text":
                return json.loads(block["text"])
        return {}


# ═══════════════════════════════════════════════════════════
#  OCR 眼睛 — easyocr 轻量模型
# ═══════════════════════════════════════════════════════════

class OcrEye:
    """视频 Agent 的眼睛。用 easyocr 本地跑，不用大模型。"""

    def __init__(self):
        self._reader = None

    def _ensure_reader(self):
        if self._reader is None:
            import easyocr
            print("[OCR] 正在加载 easyocr 模型（首次运行会下载）...")
            self._reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
            print("[OCR] 模型就绪")

    def read(self, image_path: str) -> str:
        """读取图片中的文字，返回拼接后的文本。"""
        self._ensure_reader()
        t0 = time.time()
        results = self._reader.readtext(image_path, detail=0)
        elapsed = time.time() - t0
        text = " ".join(results)
        print(f"  [OCR] {elapsed:.1f}s → {len(results)} 个文本块: {text[:120]}...")
        return text


# ═══════════════════════════════════════════════════════════
#  Agent 主循环 — 我就是 Agent
# ═══════════════════════════════════════════════════════════

async def run_agent():
    print("=" * 60)
    print("  多模态视频 Agent -- 端到端测试")
    print("  眼睛: easyocr | 耳朵: live_caption (Whisper)")
    print("  统一度量衡: video.currentTime")
    print("=" * 60)

    # 1. 启动 MCP Server
    print("\n[1/6] 启动 MCP Server...")
    client = McpClient(SERVER_PATH)
    await client.start()
    tools = AgentToolkit(client)
    ocr = OcrEye()

    try:
        # 2. 导航到视频
        print("\n[2/6] 导航到 B站视频...")
        url = "https://www.bilibili.com/video/BV1nG4y1p7rE"
        result = await tools.navigate(url)
        print(f"  导航结果: {json.dumps(result, ensure_ascii=False)}")

        await asyncio.sleep(3)  # 等页面加载完

        # 3. 获取视频信息
        print("\n[3/6] 获取视频状态...")
        state = await tools.get_state()
        print(f"  视频时长: {state.get('duration', '?')}s")
        print(f"  readyState: {state['videos'][0].get('readyState', '?') if state.get('videos') else '?'}")
        print(f"  currentTime: {state['videos'][0].get('currentTime', '?') if state.get('videos') else '?'}")

        # 4. 播放 + 暂停 + 截图 循环
        print("\n[4/6] 粗扫描：播放 → 暂停 → 截图 → OCR...")
        await tools.play()
        await asyncio.sleep(8)  # 播放 8 秒
        await tools.pause()
        await asyncio.sleep(1)

        state = await tools.get_state()
        video_time = state["videos"][0].get("currentTime", 0) if state.get("videos") else 0
        print(f"\n  暂停在: T_video={video_time:.1f}s")

        # 截图
        filepath, meta = await tools.screenshot()
        print(f"  截图: {filepath}")

        if filepath and os.path.exists(filepath):
            ocr_text = ocr.read(filepath)
            print(f"\n  ┌─ 时间戳锚点: T_video={video_time:.1f}s")
            print(f"  ├─ 截图: {os.path.basename(filepath)}")
            print(f"  └─ OCR结果: {ocr_text[:200]}")

        # 5. seek 到不同时间点验证时间戳导航
        print("\n[5/6] 验证时间戳导航: seek → 截图 → OCR...")
        targets = [60, 120, 180]

        for t in targets:
            print(f"\n  --- seek({t}s) ---")
            seek_result = await tools.seek(t)
            drift = seek_result.get("drift", "?")
            method = seek_result.get("method", "?")
            actual = seek_result.get("actualTime", t)
            print(f"    drift={drift}s | method={method} | actualTime={actual}s")

            await asyncio.sleep(0.5)

            # 截图
            filepath, meta = await tools.screenshot()
            state = await tools.get_state()
            video_time = state["videos"][0].get("currentTime", 0) if state.get("videos") else t

            if filepath and os.path.exists(filepath):
                ocr_text = ocr.read(filepath)
                print(f"    [T_video={video_time:.1f}s] OCR: {ocr_text[:100]}")

        # 6. 总结
        print(f"\n[6/6] {'='*60}")
        print("  端到端链路验证完成！")
        print()
        print("  统一度量衡生效方式:")
        print("    字幕[120s] → LLM读到 → seek(120) → pause → screenshot → OCR")
        print("    时间戳 = 坐标，指哪打哪")
        print()
        print("  成本分析:")
        print("    Whisper 转录: 本地跑，免费")
        print("    easyocr: 本地跑，免费")
        print("    LLM: 只读文本，不读图，token 极少")
        print("    = 几乎零成本看视频")
        print("=" * 60)

    finally:
        print("\n[清理] 关闭 MCP Server...")
        await client.close()
        print("[清理] 完成")


if __name__ == "__main__":
    asyncio.run(run_agent())
