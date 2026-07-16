"""
Agent v8 — 去重防循环 + Cookie 登录 + LLM 延迟模拟 + 截图语义命名
"""
import asyncio, json, os, sys, subprocess, time, re, glob, random

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(ROOT, "dist", "index.js")
CAPTION_SCRIPT = os.path.join(ROOT, "live_caption_video.py")
URL = "https://www.bilibili.com/video/BV1mxb3zKEzL/?spm_id_from=333.337.search-card.all.click&vd_source=65647d080500ee5a6f027fa53c67a0de"
COOKIES = ""  # 设为你自己的 B站 Cookie，或留空

def read_caps(path, tail=40):
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f: lines=f.readlines()
    caps=[]
    for l in lines:
        m=re.match(r"\[T=([\d.]+)s\]\s+(.+)",l)
        if m: caps.append((float(m.group(1)),m.group(2)))
    return caps[-tail:]

# ═══════════════════════════════════════════════════════════
#  LLM 监听器 + 去重
# ═══════════════════════════════════════════════════════════

class LLMListens:
    def __init__(self):
        # 去重: 已处理的视频时间区间，避免回退后重复匹配
        self.processed = []  # [(start, end), ...]
        # 已见过的章节标签，每个标签只触发一次
        self.seen_labels = set()
        # 触发词 → 标签 映射
        self.triggers = [
            (["大家好","欢迎","各位同学","哈喽","Hello"], "开场介绍"),
            (["期刊","投稿","MDPI"], "期刊背景"),
            (["流程","步骤","第一步","第二步"], "投稿流程"),
            (["案例","实例","举例","实战"], "案例分析"),
            (["总结","回顾","最后","结尾"], "总结回顾"),
            (["架构","系统","设计"], "技术架构"),
            (["影响因子","自引率","预警","版面费"], "期刊数据"),
        ]

    def is_processed(self, vt: float) -> bool:
        return any(s <= vt <= e for s, e in self.processed)

    def mark_processed(self, vt: float, window: float = 20):
        """标记 vt 前后各 window/2 秒为已处理"""
        half = window / 2
        self.processed.append((max(0, vt - half), vt + half))

    async def listen_and_decide(self, caps_vt, tools, duration):
        """边读字幕边决策，已处理的时间区间自动跳过"""
        decisions = []
        for vt_t, txt in caps_vt:
            # 去重检查
            if self.is_processed(vt_t):
                continue

            for keywords, label in self.triggers:
                if label in self.seen_labels:
                    continue
                if any(kw in txt for kw in keywords):
                    # ── LLM 延迟 ──
                    delay = random.uniform(1.0, 2.5)
                    print(f"\n  [LLM] 听到「{keywords[0]}」@ T={vt_t:.0f}s → 思考 {delay:.1f}s")
                    await asyncio.sleep(delay)

                    # ── 工具调用额外延迟 ──
                    await asyncio.sleep(random.uniform(0.3, 0.8))

                    fname = f"{vt_t:.0f}s_{label}"
                    print(f"  [Agent] capture_at({vt_t:.0f}s, name='{fname}')")

                    res = await tools.capture_at(vt_t, fname)
                    drift = res.get("drift","?")
                    reliable = res.get("reliable", False)
                    print(f"  [Agent] drift={drift}s, {'OK' if reliable else 'UNRELIABLE'}, 字幕: {txt[:70]}")

                    # 标记已处理 + 已见标签
                    self.mark_processed(vt_t)
                    self.seen_labels.add(label)
                    decisions.append({"time": vt_t, "label": fname, "drift": drift})
                    break  # 一行字幕最多匹配一个标签
        return decisions


# ═══════════════════════════════════════════════════════════
#  MCP 客户端
# ═══════════════════════════════════════════════════════════

class McpClient:
    def __init__(self,p): self.p=p; self.proc=None; self._id=0
    async def start(self):
        self.proc=subprocess.Popen(["D:\\Program Files\\nodejs\\node.exe", self.p], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await self._r("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent","version":"8.0"}})
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
    async def nav(s): return pt(await s.c.call("video_navigate",{"url":URL,"cookies":COOKIES}))
    async def state(s): return pt(await s.c.call("video_get_state",{}))
    async def play(s): return pt(await s.c.call("video_play",{}))
    async def pause(s): return pt(await s.c.call("video_pause",{}))
    async def capture_at(s, sec, name):
        return pt(await s.c.call("video_capture_at", {"seconds": sec, "name": name}))


# ═══════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════

async def main():
    today = time.strftime('%Y%m%d')
    for f in glob.glob(os.path.join(ROOT, f"captions_{today}*.txt")):
        try: os.remove(f)
        except: pass

    print("[1] 启动耳朵...")
    cap_proc = subprocess.Popen([sys.executable, CAPTION_SCRIPT], cwd=ROOT)
    cap_log = os.path.join(ROOT, f"captions_{time.strftime('%Y%m%d')}.txt")
    await asyncio.sleep(8)

    print("[2] 启动手 (MCP + Cookie)...")
    c = McpClient(SERVER_PATH); await c.start()
    t = T(c)

    try:
        print("[3] 导航 (带 Cookie 登录)...")
        await t.nav()
        await asyncio.sleep(6)
        st = await t.state()
        dur = st.get("duration",0)
        vt0 = st["videos"][0].get("currentTime",0) if st.get("videos") else 0

        caps_before = read_caps(cap_log, tail=3)
        cap_offset = caps_before[-1][0] if caps_before else 0
        print(f"  时长={dur:.0f}s | T={vt0:.1f}s | 字幕偏移={cap_offset:.1f}s")
        print(f"  Cookie 已注入")

        await t.play()
        print("[4] 播放中，Agent 监听...")

        llm = LLMListens()
        elapsed = 0
        check_times = [15, 40, 70, 100, 140, 180]
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
            print(f"  T_video={vt:.1f}s | 已处理区间: {[(round(s),round(e)) for s,e in llm.processed]}")

            caps = read_caps(cap_log, tail=40)
            caps_vt = [(ctt - cap_offset, txt) for ctt, txt in caps if ctt >= cap_offset and txt.strip()]
            # 过滤已处理的
            new_caps = [(ct2, txt) for ct2, txt in caps_vt if not llm.is_processed(ct2)]
            skipped = len(caps_vt) - len(new_caps)

            if new_caps:
                print(f"  新字幕 {len(new_caps)} 行 (跳过 {skipped} 行已处理):")
                for ct2, txt in new_caps[-5:]:
                    print(f"    [{ct2:.0f}s] {txt[:80]}")

                await llm.listen_and_decide(new_caps, t, dur)

                # 检查是否所有标签都已触发
                all_triggered = len(llm.seen_labels) >= len(llm.triggers)
                print(f"  已触发标签: {llm.seen_labels}")
                if all_triggered:
                    print("  所有标签已触发，跳过后续检查")
            else:
                print(f"  (字幕全部已处理，跳过 {skipped} 行)")

            if vt >= (dur or 999) * 0.85:
                break

            await t.play()

        # ── 5. 总结 ──
        print("\n" + "="*60)
        print("  端到端 Agent v8 运行完成")
        print(f"  已处理区间: {[(round(s),round(e)) for s,e in llm.processed]}")
        print(f"  已触发标签: {llm.seen_labels}")
        print()
        print("  核心改进:")
        print("    1. Cookie 登录态注入")
        print("    2. 去重: 已处理时间区间自动跳过")
        print("    3. 每章节目录只触发一次")
        print("    4. LLM 延迟 + seek back + capture_at 语义命名")
        print("="*60)

    finally:
        cap_proc.terminate(); cap_proc.wait(timeout=5)
        await c.close()

if __name__=="__main__":
    asyncio.run(main())
