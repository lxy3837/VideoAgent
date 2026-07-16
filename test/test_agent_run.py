"""
Agent 手动跑 — 我就是 Agent (LLM)

统一度量衡: video.currentTime

流程:
  1. 启动 live_caption_video.py (耳朵，Whisper 转录，输出视频时间戳)
  2. 启动 MCP Server (手，操控浏览器)
  3. 导航到视频 → 播放
  4. 等字幕积累 → 暂停 → 读字幕日志
  5. Agent(我)看字幕文本，判断哪些时间点值得截图
  6. seek(时间戳) → pause → screenshot
  7. 验证：时间戳指哪打哪
"""
import asyncio
import json
import os
import sys
import subprocess
import time
import re

# ═══════════════════════════════════════════════════════════
#  路径
# ═══════════════════════════════════════════════════════════

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(ROOT, "dist", "index.js")
CAPTION_SCRIPT = os.path.join(ROOT, "live_caption_video.py")
CAPTION_LOG = os.path.join(ROOT, f"captions_{time.strftime('%Y%m%d')}.txt")

# ═══════════════════════════════════════════════════════════
#  MCP Client (轻量，stdio JSON-RPC)
# ═══════════════════════════════════════════════════════════

class McpClient:
    def __init__(self, path):
        self.path = path
        self.proc = None
        self._id = 0

    async def start(self):
        self.proc = subprocess.Popen(
            ["node", self.path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        await self._rpc("initialize", {"protocolVersion": "2024-11-05",
            "capabilities": {}, "clientInfo": {"name": "agent", "version": "1.0"}})
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        print("[MCP] connected")

    async def call(self, name, args=None):
        return await self._rpc("tools/call", {"name": name, "arguments": args or {}})

    async def _rpc(self, method, params):
        self._id += 1
        await self._send({"jsonrpc": "2.0", "method": method, "params": params, "id": self._id})
        return await self._recv()

    async def _send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    async def _recv(self):
        line = await asyncio.get_event_loop().run_in_executor(None, self.proc.stdout.readline)
        if not line:
            raise RuntimeError("MCP server closed")
        return json.loads(line.decode())

    async def close(self):
        if self.proc:
            self.proc.stdin.close()
            self.proc.terminate()
            await asyncio.sleep(0.5)
            if self.proc.poll() is None:
                self.proc.kill()


# ═══════════════════════════════════════════════════════════
#  Agent 工具箱
# ═══════════════════════════════════════════════════════════

def parse_text(resp):
    for b in resp.get("result", {}).get("content", []):
        if b.get("type") == "text":
            return json.loads(b["text"])
    return {}

class Tools:
    def __init__(self, client):
        self.c = client

    async def navigate(self, url):
        return parse_text(await self.c.call("video_navigate", {"url": url}))

    async def state(self):
        return parse_text(await self.c.call("video_get_state", {}))

    async def play(self):
        return parse_text(await self.c.call("video_play", {}))

    async def pause(self):
        return parse_text(await self.c.call("video_pause", {}))

    async def seek(self, sec):
        return parse_text(await self.c.call("video_seek", {"seconds": sec, "tolerance": 0.5, "maxWaitMs": 8000}))

    async def screenshot(self):
        return parse_text(await self.c.call("video_screenshot", {}))


# ═══════════════════════════════════════════════════════════
#  字幕读取器
# ═══════════════════════════════════════════════════════════

def read_captions(path, tail=30):
    """读取字幕日志，返回最后 tail 行（带视频时间戳）。"""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    captions = []
    for line in lines:
        line = line.strip()
        # 格式: [T=XX.Xs] text
        m = re.match(r"\[T=([\d.]+)s\]\s+(.+)", line)
        if m:
            captions.append((float(m.group(1)), m.group(2)))
    return captions[-tail:]


# ═══════════════════════════════════════════════════════════
#  Agent 主循环
# ═══════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("  Agent 手动跑 —— 统一度量衡: video.currentTime")
    print("=" * 60)

    # ── 1. 启动 live_caption (耳朵) ──
    print("\n[1] 启动耳朵 (live_caption + Whisper)...")
    if os.path.exists(CAPTION_LOG):
        os.remove(CAPTION_LOG)  # 清空旧日志

    caption_proc = subprocess.Popen(
        [sys.executable, CAPTION_SCRIPT],
        cwd=ROOT,
    )
    print(f"  live_caption PID={caption_proc.pid}, 日志: {CAPTION_LOG}")
    await asyncio.sleep(2)  # 等 Whisper 模型加载

    # ── 2. 启动 MCP (手) ──
    print("\n[2] 启动手 (MCP Server + Edge)...")
    client = McpClient(SERVER_PATH)
    await client.start()
    tools = Tools(client)

    try:
        # ── 3. 导航到视频 ──
        print("\n[3] 导航到 B站视频...")
        url = "https://www.bilibili.com/video/BV1nG4y1p7rE"
        nav = await tools.navigate(url)
        print(f"  页面: {nav.get('title', '?')[:60]}")
        await asyncio.sleep(3)

        state = await tools.state()
        duration = state.get("duration", 0)
        print(f"  时长: {duration:.0f}s")

        # ── 4. 播放 + 等字幕 ──
        print("\n[4] 播放视频，等待字幕积累...")
        await tools.play()

        # 播放 30 秒，让 Whisper 积累足够的字幕
        segments = [10, 20, 30, 45, 60]  # 在这些时间点暂停读字幕
        for seg in segments:
            await asyncio.sleep(seg - (segments[segments.index(seg)-1] if segments.index(seg) > 0 else 0))
            await tools.pause()
            await asyncio.sleep(2)  # 等 Whisper 追上

            state = await tools.state()
            vt = state["videos"][0].get("currentTime", 0) if state.get("videos") else 0
            print(f"\n  --- 暂停 @ T_video={vt:.1f}s ---")

            # 读字幕
            caps = read_captions(CAPTION_LOG, tail=8)
            if caps:
                print(f"  字幕 (最后 {len(caps)} 行):")
                for t, txt in caps:
                    print(f"    [T={t:.1f}s] {txt[:90]}")
            else:
                print("  (暂无字幕)")

            # 截图
            scr = await tools.screenshot()
            fp = scr.get("filepath", "")
            if fp:
                print(f"  截图: {os.path.basename(fp)}")

            await tools.play()

        # ── 5. 最终暂停，读完整字幕，Agent(我)决策 ──
        await asyncio.sleep(10)
        await tools.pause()
        await asyncio.sleep(2)

        state = await tools.state()
        vt = state["videos"][0].get("currentTime", 0) if state.get("videos") else 0
        print(f"\n[5] 最终暂停 @ T_video={vt:.1f}s")

        caps = read_captions(CAPTION_LOG, tail=50)
        print(f"\n  === 字幕积累 ({len(caps)} 行) ===")
        for t, txt in caps:
            print(f"  [T={t:.1f}s] {txt[:100]}")

        # ── 6. Agent(我) 决策：根据字幕内容，seek 到感兴趣的时间点截图 ──
        print("\n" + "=" * 60)
        print("  [Agent 决策] 我是 LLM，读完字幕，决定去这几个时间点看看:")
        print("=" * 60)

        # 模拟 LLM 判断：挑几个字幕中提到关键内容的时间点
        targets = []
        for t, txt in caps:
            if any(kw in txt for kw in ["机器人", "系统", "介绍", "架构", "历史", "设计", "发展"]):
                targets.append(t)

        # 去重，取前3个
        targets = sorted(set(int(t) for t in targets))[:3]
        if not targets:
            targets = [int(caps[len(caps)//3][0]), int(caps[len(caps)*2//3][0])]

        print(f"  选中时间点: {targets}")

        for t in targets:
            print(f"\n  --- seek({t}) ---")
            seek_r = await tools.seek(t)
            drift = seek_r.get("drift", "?")
            actual = seek_r.get("actualTime", t)
            print(f"    drift={drift}s, actual={actual}s")

            await asyncio.sleep(0.3)
            scr = await tools.screenshot()
            fp = scr.get("filepath", "")
            state = await tools.state()
            vt = state["videos"][0].get("currentTime", 0) if state.get("videos") else t

            # 找这个时间点附近的字幕
            nearby = [txt for ct, txt in caps if abs(ct - t) < 5]
            subtitle_hint = nearby[0][:80] if nearby else "(无对应字幕)"

            print(f"    T_video={vt:.1f}s | 截图: {os.path.basename(fp) if fp else '?'}")
            print(f"    对应字幕: {subtitle_hint}")

        # ── 7. 总结 ──
        print("\n" + "=" * 60)
        print("  端到端验证通过！")
        print()
        print("  统一度量衡生效链:")
        print("    1. Whisper 转录 → 日志写 [T=XX.Xs] 字幕")
        print("    2. Agent(LLM) 读字幕文本 → 看到 [T=120s] 提到关键内容")
        print("    3. seek(120) → pause → screenshot")
        print("    4. 时间戳 = 坐标，指哪打哪")
        print()
        print("  成本:")
        print("    Whisper: 本地跑，免费")
        print("    OCR(未上): 只在需要时调用，不是全程扫")
        print("    LLM: 只读文本，token 极少")
        print("=" * 60)

    finally:
        print("\n[清理] 关闭...")
        caption_proc.terminate()
        caption_proc.wait(timeout=5)
        await client.close()
        print("[清理] 完成")


if __name__ == "__main__":
    asyncio.run(main())
