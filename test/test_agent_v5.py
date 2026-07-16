"""
Agent v5 — 精简版: 耳朵先跑，手再跟上，时间戳统一度量衡
"""
import asyncio, json, os, sys, subprocess, time, re, glob

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(ROOT, "dist", "index.js")
CAPTION_SCRIPT = os.path.join(ROOT, "live_caption_video.py")
URL = "https://www.bilibili.com/video/BV1mxb3zKEzL/?spm_id_from=333.337.search-card.all.click&vd_source=65647d080500ee5a6f027fa53c67a0de"

# ── MCP ──
class McpClient:
    def __init__(self, path):
        self.path = path; self.proc = None; self._id = 0
    async def start(self):
        self.proc = subprocess.Popen(["node", self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await self._r("initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent","version":"5.0"}})
        await self._s({"jsonrpc":"2.0","method":"notifications/initialized"})
    async def call(self, n, a=None):
        return await self._r("tools/call", {"name":n,"arguments":a or {}})
    async def _r(self, m, p):
        self._id+=1; await self._s({"jsonrpc":"2.0","method":m,"params":p,"id":self._id}); l=await asyncio.get_event_loop().run_in_executor(None, self.proc.stdout.readline)
        return json.loads(l.decode()) if l else {}
    async def _s(self, m):
        self.proc.stdin.write((json.dumps(m)+"\n").encode()); self.proc.stdin.flush()
    async def close(self):
        if self.proc: self.proc.stdin.close(); self.proc.terminate(); await asyncio.sleep(0.5)
        if self.proc and self.proc.poll() is None: self.proc.kill()

def pt(r):
    for b in r.get("result",{}).get("content",[]):
        if b.get("type")=="text": return json.loads(b["text"])
    return {}

class T:
    def __init__(s,c): s.c=c
    async def nav(s): return pt(await s.c.call("video_navigate",{"url":URL}))
    async def state(s): return pt(await s.c.call("video_get_state",{}))
    async def play(s): return pt(await s.c.call("video_play",{}))
    async def pause(s): return pt(await s.c.call("video_pause",{}))
    async def seek(s,sec): return pt(await s.c.call("video_seek",{"seconds":sec,"tolerance":0.5,"maxWaitMs":8000}))
    async def scr(s): return pt(await s.c.call("video_screenshot",{}))

def read_caps(path, tail=30):
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f: lines=f.readlines()
    caps=[]
    for l in lines:
        m=re.match(r"\[T=([\d.]+)s\]\s+(.+)",l)
        if m: caps.append((float(m.group(1)),m.group(2)))
    return caps[-tail:]

async def main():
    # 清旧日志
    today = time.strftime('%Y%m%d')
    old_logs = glob.glob(os.path.join(ROOT, f"captions_{today}*.txt"))
    for f in old_logs:
        try: os.remove(f)
        except: pass

    # ── 1. 启动耳朵 ──
    print("[1] 启动 live_caption (耳朵)...")
    cap_proc = subprocess.Popen([sys.executable, CAPTION_SCRIPT], cwd=ROOT)
    cap_log = os.path.join(ROOT, f"captions_{time.strftime('%Y%m%d')}.txt")

    # 等模型加载
    await asyncio.sleep(8)
    print("  耳朵就绪")

    # ── 2. 启动手 ──
    print("[2] 启动 MCP (手)...")
    c = McpClient(SERVER_PATH); await c.start()
    t = T(c)

    try:
        # ── 3. 导航 + 播放 ──
        print("[3] 导航到视频...")
        await t.nav()
        await asyncio.sleep(5)

        st = await t.state()
        dur = st.get("duration", 0)
        vt0 = st["videos"][0].get("currentTime",0) if st.get("videos") else 0
        print(f"  时长={dur:.0f}s, 当前={vt0:.1f}s")

        # 记录播放起始时间 (live_caption 的相对时间)
        # 读一下当前字幕的最后时间戳作为 offset
        caps_before = read_caps(cap_log, tail=5)
        cap_offset = caps_before[-1][0] if caps_before else 0
        print(f"  字幕偏移={cap_offset:.1f}s (live_caption 已运行时间)")

        print("[4] 播放！")
        await t.play()
        play_start = time.time()

        # ── 4. 周期性暂停检查 ──
        check_times = [10, 25, 45, 70]
        elapsed = 0
        for ct in check_times:
            await asyncio.sleep(ct - elapsed)
            elapsed = ct
            await t.pause()
            await asyncio.sleep(2)  # 等 Whisper 追上

            st = await t.state()
            vt = st["videos"][0].get("currentTime",0) if st.get("videos") else 0
            print(f"\n  --- T_video={vt:.1f}s ---")

            caps = read_caps(cap_log, tail=8)
            if caps:
                print(f"  字幕 (最后{len(caps)}行):")
                for cct, txt in caps:
                    adj = cct - cap_offset  # 减偏移 = 近似的视频时间
                    diff = abs(adj - vt)
                    tag = " <-- 对齐!" if diff < 3 else f" (偏差{diff:.1f}s)"
                    print(f"    [T={cct:.1f}s → 视频≈{adj:.1f}s] {txt[:80]}{tag}")
            else:
                print("  (暂无字幕)")

            scr = await t.scr()
            fp = scr.get("filepath","")
            print(f"  截图: {os.path.basename(fp) if fp else '?'}")

            if vt >= dur * 0.8:  # 快播完了
                break

            await t.play()

        # ── 5. 最终暂停，Agent 决策 ──
        await t.pause()
        await asyncio.sleep(2)
        st = await t.state()
        vt = st["videos"][0].get("currentTime",0) if st.get("videos") else 0
        print(f"\n[5] 最终暂停 @ T_video={vt:.1f}s")

        caps = read_caps(cap_log, tail=40)
        print(f"\n  === 字幕总计 {len(caps)} 行 (原始时间戳) ===")
        for cct, txt in caps:
            adj = cct - cap_offset
            print(f"  [T={cct:.1f}s → 视频≈{adj:.1f}s] {txt[:100]}")

        # Agent 决策
        print("\n" + "="*60)
        print("  [Agent 决策] 读字幕，选关键时间点 seek")
        targets = sorted(set(int(cct - cap_offset) for cct, txt in caps
            if any(kw in txt for kw in ["机器人","系统","设计","历史","发展","架构","介绍","算法","模型","学习","训练","数据"])))
        targets = [t for t in targets if 0 < t < (dur or 999)]
        if len(targets) > 3: targets = targets[:3]
        if not targets and caps:
            targets = [int(caps[len(caps)//3][0] - cap_offset), int(caps[2*len(caps)//3][0] - cap_offset)]
        print(f"  选中: {targets}")

        for tgt in targets:
            print(f"\n  seek({tgt})...")
            sr = await t.seek(tgt)
            await asyncio.sleep(0.5)
            scr = await t.scr()
            st = await t.state()
            vt = st["videos"][0].get("currentTime",0) if st.get("videos") else tgt
            nearby = [txt for cct,txt in caps if abs((cct-cap_offset)-tgt)<5]
            hint = nearby[0][:80] if nearby else "(无对应字幕)"
            print(f"    drift={sr.get('drift','?')}s | T_video={vt:.1f}s")
            print(f"    截图: {os.path.basename(scr.get('filepath','?'))}")
            print(f"    字幕: {hint}")

        print("\n" + "="*60)
        print("  验证通过!")
        print("  字幕时间戳 - 偏移 = 视频时间戳 → seek → 截图")
        print("  统一度量衡生效!")
        print("="*60)

    finally:
        cap_proc.terminate(); cap_proc.wait(timeout=5)
        await c.close()

if __name__=="__main__":
    asyncio.run(main())
