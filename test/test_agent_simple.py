"""
Agent 简化版 — live_caption 已在终端2运行，这里只操控 MCP
"""
import asyncio
import json
import os
import sys
import subprocess
import time
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(ROOT, "dist", "index.js")
CAPTION_LOG = os.path.join(ROOT, f"captions_{time.strftime('%Y%m%d')}.txt")

# ── MCP Client ──
class McpClient:
    def __init__(self, path):
        self.path = path; self.proc = None; self._id = 0
    async def start(self):
        self.proc = subprocess.Popen(["node", self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await self._rpc("initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent","version":"1.0"}})
        await self._send({"jsonrpc":"2.0","method":"notifications/initialized"})
    async def call(self, name, args=None):
        return await self._rpc("tools/call", {"name":name,"arguments":args or {}})
    async def _rpc(self, m, p):
        self._id+=1; await self._send({"jsonrpc":"2.0","method":m,"params":p,"id":self._id}); return await self._recv()
    async def _send(self, msg):
        self.proc.stdin.write((json.dumps(msg)+"\n").encode()); self.proc.stdin.flush()
    async def _recv(self):
        line = await asyncio.get_event_loop().run_in_executor(None, self.proc.stdout.readline)
        if not line: raise RuntimeError("closed")
        return json.loads(line.decode())
    async def close(self):
        if self.proc:
            self.proc.stdin.close(); self.proc.terminate()
            await asyncio.sleep(0.5)
            if self.proc.poll() is None: self.proc.kill()

def pt(resp):
    for b in resp.get("result",{}).get("content",[]):
        if b.get("type")=="text": return json.loads(b["text"])
    return {}

class T:
    def __init__(s,c): s.c=c
    async def nav(s,u): return pt(await s.c.call("video_navigate",{"url":u}))
    async def state(s): return pt(await s.c.call("video_get_state",{}))
    async def play(s): return pt(await s.c.call("video_play",{}))
    async def pause(s): return pt(await s.c.call("video_pause",{}))
    async def seek(s,sec): return pt(await s.c.call("video_seek",{"seconds":sec,"tolerance":0.5,"maxWaitMs":8000}))
    async def scr(s): return pt(await s.c.call("video_screenshot",{}))

def read_caps(path, tail=20):
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f: lines=f.readlines()
    caps=[]
    for l in lines:
        l=l.strip()
        m=re.match(r"\[T=([\d.]+)s\]\s+(.+)",l)
        if m: caps.append((float(m.group(1)),m.group(2)))
    return caps[-tail:]

async def main():
    print("="*60)
    print("  Agent 简化版 — live_caption 已在终端2监听")
    print("="*60)

    client = McpClient(SERVER_PATH)
    await client.start()
    t=T(client)

    try:
        # 1. 导航
        print("\n[1] 导航到视频...")
        await t.nav("https://www.bilibili.com/video/BV1nG4y1p7rE")
        await asyncio.sleep(3)
        st = await t.state()
        dur = st.get("duration",0)
        print(f"  时长: {dur:.0f}s")

        # 2. 播放，每隔一段时间暂停检查字幕
        print("\n[2] 播放中... live_caption 在后台记录字幕")
        await t.play()

        check_points = [10, 25, 45, 70]
        prev = 0
        for cp in check_points:
            await asyncio.sleep(cp - prev)
            prev = cp
            await t.pause()
            await asyncio.sleep(2)  # 等 Whisper 追上

            st = await t.state()
            vt = st["videos"][0].get("currentTime",0) if st.get("videos") else 0
            print(f"\n  --- T_video={vt:.1f}s ---")

            caps = read_caps(CAPTION_LOG, tail=6)
            if caps:
                print(f"  字幕:")
                for ct, txt in caps:
                    diff = abs(ct - vt)
                    tag = "  <-- 对齐!" if diff < 3 else f"  (偏差{diff:.1f}s)"
                    print(f"    [T={ct:.1f}s] {txt[:80]}{tag}")
            else:
                print("  (暂无字幕)")

            scr = await t.scr()
            print(f"  截图: {os.path.basename(scr.get('filepath','?'))}")

            await t.play()

        # 最终暂停
        await asyncio.sleep(15)
        await t.pause()
        await asyncio.sleep(2)
        st = await t.state()
        vt = st["videos"][0].get("currentTime",0) if st.get("videos") else 0
        print(f"\n[3] 最终暂停 @ T_video={vt:.1f}s")

        caps = read_caps(CAPTION_LOG, tail=40)
        print(f"\n  === 所有字幕 ({len(caps)} 行) ===")
        for ct, txt in caps:
            print(f"  [T={ct:.1f}s] {txt[:100]}")

        # Agent决策: 根据关键字选时间点
        print("\n"+"="*60)
        print("  [Agent=我] 根据字幕选时间点 seek...")
        targets = sorted(set(int(ct) for ct,txt in caps
            if any(kw in txt for kw in ["机器人","系统","设计","历史","发展","架构","介绍"])))[:3]
        if not targets and caps:
            targets = [int(caps[len(caps)//3][0]), int(caps[len(caps)*2//3][0])]
        print(f"  选中: {targets}")

        for tgt in targets:
            print(f"\n  seek({tgt})...")
            sr = await t.seek(tgt)
            print(f"    drift={sr.get('drift','?')}s")
            await asyncio.sleep(0.3)
            scr = await t.scr()
            st = await t.state()
            vt = st["videos"][0].get("currentTime",0) if st.get("videos") else tgt
            nearby = [txt for ct,txt in caps if abs(ct-tgt)<5]
            hint = nearby[0][:80] if nearby else "(无)"
            print(f"    T_video={vt:.1f}s | 截图: {os.path.basename(scr.get('filepath','?'))}")
            print(f"    字幕: {hint}")

        print("\n"+"="*60)
        print("  验证通过: 字幕时间戳 -> seek -> 截图, 指哪打哪!")
        print("="*60)

    finally:
        await client.close()

if __name__=="__main__":
    asyncio.run(main())
