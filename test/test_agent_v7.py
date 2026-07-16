"""
Agent v7 — 真实 LLM 延迟模拟 + Agent 调用截图时直接命名

核心模拟:
  1. Agent 边播边看字幕流 → "听到关键词"
  2. 模拟 LLM 思考 1-2s (API 延迟) → 视频已经播过头了
  3. seek 回目标时间 → video_capture_at(name=语义标签)
  4. 截图直接以语义标签命名，不走后重命名
"""
import asyncio, json, os, sys, subprocess, time, re, glob, random

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(ROOT, "dist", "index.js")
CAPTION_SCRIPT = os.path.join(ROOT, "live_caption_video.py")
URL = "https://www.bilibili.com/video/BV1mxb3zKEzL/?spm_id_from=333.337.search-card.all.click&vd_source=65647d080500ee5a6f027fa53c67a0de"

# ── 工具函数 ──
def read_caps(path, tail=40):
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f: lines=f.readlines()
    caps=[]
    for l in lines:
        m=re.match(r"\[T=([\d.]+)s\]\s+(.+)",l)
        if m: caps.append((float(m.group(1)),m.group(2)))
    return caps[-tail:]

def sanitize(s):
    return re.sub(r'[<>:"/\\|?*]','_', s)[:80]

# ═══════════════════════════════════════════════════════════
#  LLM 模拟器 — 带真实决策延迟
# ═══════════════════════════════════════════════════════════

class LLMListens:
    """
    模拟真实 LLM 工作流:
    1. LLM 边读字幕边建关键词匹配（这步实际也是 LLM 做，但这里用 rule 模拟）
    2. 匹配到关键词后 → 1~2s API 调用延迟
    3. 延迟期间视频继续播 → 决策下来后 seek 回 target 时间
    4. 调用 video_capture_at(name=语义标签)
    """

    async def listen_and_decide(self, caps_vt, tools, duration):
        """
        边"听"字幕边做决策，模拟 LLM 延迟。
        caps_vt: [(视频时间, 文本), ...]  已按时间排序
        tools:   T 实例（MCP 工具调用）
        """
        decisions = []
        # 模拟分段触发: 每个关键节点触发一次
        triggers = [
            (["大家好","欢迎","各位"], "开场介绍"),
            (["期刊","投稿","MDPI"], "期刊背景"),
            (["流程","步骤"], "投稿流程"),
            (["案例","实例","举例"], "案例分析"),
            (["总结","回顾"], "总结回顾"),
            (["架构","系统","设计"], "技术架构"),
        ]

        seen_labels = set()

        for vt_t, txt in caps_vt:
            for keywords, label in triggers:
                if label in seen_labels:
                    continue
                if any(kw in txt for kw in keywords):
                    # ── LLM 延迟: 1~2s ──
                    delay = random.uniform(1.0, 2.5)
                    print(f"\n  [LLM] 听到关键词「{keywords[0]}」@ T={vt_t:.0f}s")
                    print(f"  [LLM] 思考中... ({delay:.1f}s)")
                    await asyncio.sleep(delay)

                    # 此时视频已经播过头了，需要 seek 回去
                    # 模拟工具调用延迟 0.3~0.8s
                    tool_delay = random.uniform(0.3, 0.8)
                    await asyncio.sleep(tool_delay)

                    # 构建语义文件名
                    fname = f"{vt_t:.0f}s_{label}"

                    print(f"  [Agent] 决定截图 → seek({vt_t:.0f}) → capture_at(name='{fname}')")

                    # 调用 video_capture_at（一键 seek+截图+命名）
                    res = await tools.capture_at(vt_t, fname)
                    drift = res.get("drift","?")
                    reliable = res.get("reliable", False)

                    print(f"  [Agent] drift={drift}s, reliable={reliable}")
                    print(f"  [Agent] 字幕: {txt[:80]}")

                    seen_labels.add(label)
                    decisions.append({"time": vt_t, "label": fname, "drift": drift})
                    break  # 一个 caption 最多触发一个 category

        return decisions


# ═══════════════════════════════════════════════════════════
#  MCP 客户端 + 工具包装
# ═══════════════════════════════════════════════════════════

class McpClient:
    def __init__(self,p): self.p=p; self.proc=None; self._id=0
    async def start(self):
        self.proc=subprocess.Popen(["D:\\Program Files\\nodejs\\node.exe", self.p], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await self._r("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent","version":"7.0"}})
        await self._s({"jsonrpc":"2.0","method":"notifications/initialized"})
    async def call(self,n,a=None): return await self._r("tools/call",{"name":n,"arguments":a or {}})
    async def _r(self,m,p):
        self._id+=1; await self._s({"jsonrpc":"2.0","method":m,"params":p,"id":self._id})
        l=await asyncio.get_event_loop().run_in_executor(None,self.proc.stdout.readline)
        return json.loads(l.decode()) if l else {}
    async def _s(self,m): self.proc.stdin.write((json.dumps(m)+"\n").encode()); self.proc.stdin.flush()
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
    async def capture_at(s, sec, name):
        """Agent 调用: seek + 截图 + 语义命名，一条指令完成"""
        return pt(await s.c.call("video_capture_at", {"seconds": sec, "name": name}))


# ═══════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════

async def main():
    today = time.strftime('%Y%m%d')
    for f in glob.glob(os.path.join(ROOT, f"captions_{today}*.txt")):
        try: os.remove(f)
        except: pass

    # ── 1. 耳朵 ──
    print("[1] 启动耳朵 (live_caption)...")
    cap_proc = subprocess.Popen([sys.executable, CAPTION_SCRIPT], cwd=ROOT)
    cap_log = os.path.join(ROOT, f"captions_{time.strftime('%Y%m%d')}.txt")
    await asyncio.sleep(8)

    # ── 2. 手 ──
    print("[2] 启动手 (MCP)...")
    c = McpClient(SERVER_PATH); await c.start()
    t = T(c)

    try:
        # ── 3. 导航 + 播放 ──
        print("[3] 导航...")
        await t.nav()
        await asyncio.sleep(5)
        st = await t.state()
        dur = st.get("duration",0)
        vt0 = st["videos"][0].get("currentTime",0) if st.get("videos") else 0

        caps_before = read_caps(cap_log, tail=3)
        cap_offset = caps_before[-1][0] if caps_before else 0
        print(f"  时长={dur:.0f}s | T={vt0:.1f}s | 字幕偏移={cap_offset:.1f}s")

        await t.play()
        print("[4] 播放中...")

        # ── 4. LLM 实时"监听" ──
        llm = LLMListens()
        elapsed = 0
        # 每 15s 暂停一次，读字幕，让 LLM 决策
        check_times = [15, 35, 60, 90, 120]
        for ct in check_times:
            wait = ct - elapsed
            if wait <= 0: continue
            await asyncio.sleep(wait)
            elapsed = ct

            await t.pause()
            await asyncio.sleep(1)

            st = await t.state()
            vt = st["videos"][0].get("currentTime",0) if st.get("videos") else 0
            print(f"\n{'='*50}")
            print(f"  T_video={vt:.1f}s | 暂停读字幕")

            caps = read_caps(cap_log, tail=30)
            caps_vt = [(ctt - cap_offset, txt) for ctt, txt in caps if ctt >= cap_offset and txt.strip()]

            if caps_vt:
                print(f"  新字幕 {len(caps_vt)} 行:")
                for ct2, txt in caps_vt[-5:]:
                    print(f"    [{ct2:.0f}s] {txt[:80]}")

                # LLM 实时决策（带 1~2s 延迟）
                await llm.listen_and_decide(caps_vt, t, dur)
            else:
                print("  (暂无字幕)")

            if vt >= (dur or 999) * 0.85:
                break

            await t.play()

        # ── 5. 最终总结 ──
        print("\n" + "="*60)
        print("  端到端 Agent v7 运行完成")
        print()
        print("  真实模拟了:")
        print("    1. LLM 边读字幕边监听关键词")
        print("    2. 匹配后 1~2.5s API 延迟思考")
        print("    3. 决策时视频已播过 → seek 回目标时间")
        print("    4. video_capture_at(name=语义标签) → 截图直接以语义命名")
        print()
        print("  截图文件:")
        shots_dir = os.path.join(ROOT, "screenshots")
        for f in sorted(os.listdir(shots_dir)):
            if f.endswith(".png") and not f.startswith("frame_"):
                size_kb = os.path.getsize(os.path.join(shots_dir, f)) / 1024
                print(f"    {f} ({size_kb:.0f}KB)")
        print("="*60)

    finally:
        cap_proc.terminate(); cap_proc.wait(timeout=5)
        await c.close()

if __name__=="__main__":
    asyncio.run(main())
