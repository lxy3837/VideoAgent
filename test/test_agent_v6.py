"""
Agent v6 — 模拟 LLM 决策延迟 + 截图按语义命名
"""
import asyncio, json, os, sys, subprocess, time, re, glob, shutil, random

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(ROOT, "dist", "index.js")
CAPTION_SCRIPT = os.path.join(ROOT, "live_caption_video.py")
URL = "https://www.bilibili.com/video/BV1mxb3zKEzL/?spm_id_from=333.337.search-card.all.click&vd_source=65647d080500ee5a6f027fa53c67a0de"

# ── MCP ──
class McpClient:
    def __init__(self, p): self.p=p; self.proc=None; self._id=0
    async def start(self):
        self.proc=subprocess.Popen(["node",self.p],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        await self._r("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent","version":"6.0"}})
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

def read_caps(path, tail=40):
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f: lines=f.readlines()
    caps=[]
    for l in lines:
        m=re.match(r"\[T=([\d.]+)s\]\s+(.+)",l)
        if m: caps.append((float(m.group(1)),m.group(2)))
    return caps[-tail:]

# ═══════════════════════════════════════════════════════════
#  LLM 模拟器 — 模拟真实 LLM 的决策延迟和语义理解
# ═══════════════════════════════════════════════════════════

class LLMSimulator:
    """
    模拟 LLM 的决策过程:
    1. 接收字幕文本（带时间戳）
    2. 花 1-3 秒"思考"（模拟 API 延迟）
    3. 输出: 哪些时间点值得截图 + 每个点的语义标签
    4. 模拟工具调用延迟（seek + screenshot 各需要时间）
    """

    async def analyze(self, captions: list, duration: float) -> list[dict]:
        """
        输入: [(timestamp, text), ...]
        输出: [{"time": 10.0, "label": "期刊封面介绍", "reason": "提到'O普林 sense'期刊"}, ...]
        """
        print("\n  [LLM] 正在分析字幕...")
        print(f"  [LLM] 收到 {len(captions)} 行字幕，视频时长 {duration:.0f}s")

        # 模拟 LLM API 延迟
        think_time = random.uniform(1.0, 2.5)
        print(f"  [LLM] 思考中... ({think_time:.1f}s)")
        await asyncio.sleep(think_time)

        # ── 模拟 LLM 推理 ──
        # 实际上会是这样:
        #   prompt = f"以下是视频字幕，请找出需要截图的画面，每张给出时间戳和命名"
        #   response = llm.chat(prompt, captions_text)
        targets = []

        # 按语义分段，找关键节点
        segments = self._segment(captions)

        for seg_start, seg_end, summary in segments:
            mid_time = (seg_start + seg_end) / 2
            # 生成语义标签
            label = self._gen_label(summary, mid_time)
            targets.append({
                "time": mid_time,
                "label": label,
                "reason": summary[:60],
                "range": f"{seg_start:.0f}s-{seg_end:.0f}s"
            })

        # 去重（相邻时间太近的合并）
        targets = self._dedup(targets)

        print(f"  [LLM] 决策完成，选中 {len(targets)} 个截图点:")
        for t in targets:
            print(f"    T={t['time']:.0f}s → {t['label']}")

        # 模拟"我决定调用这些工具"
        await asyncio.sleep(random.uniform(0.3, 0.8))  # 函数调用开销
        return targets

    def _segment(self, captions) -> list:
        """简单分段：按话题切换切分。"""
        if not captions: return []
        segments = []
        start_time = captions[0][0]
        current_texts = [captions[0][1]]

        for t, txt in captions[1:]:
            # 时间跨度 > 15s 或文字明显切换 → 新段
            if t - start_time > 20 or self._is_topic_switch(current_texts, txt):
                segments.append((start_time, captions[captions.index((t,txt))-1][0]
                    if captions.index((t,txt)) > 0 else t, " ".join(current_texts)))
                start_time = t
                current_texts = [txt]
            else:
                current_texts.append(txt)

        # 最后一段
        if current_texts:
            segments.append((start_time, captions[-1][0], " ".join(current_texts)))

        return segments

    def _is_topic_switch(self, prev: list, current: str) -> bool:
        """检测话题切换关键词。"""
        switches = ["首先", "接下来", "然后", "下面", "最后", "总结",
                    "另外", "此外", "第二个", "第三", "第", "好的",
                    "Hello", "大家好", "欢迎", "我们来看"]
        return any(sw in current for sw in switches)

    def _gen_label(self, summary: str, time: float) -> str:
        """根据字幕内容生成截图标签。"""
        # 模拟 LLM 理解内容并命名
        if any(kw in summary for kw in ["欢迎", "大家好", "Hello", "各位同学"]):
            return f"{time:.0f}s_课程开场介绍"
        elif any(kw in summary for kw in ["期刊", "投稿", "MDPI", "背景"]):
            return f"{time:.0f}s_期刊背景介绍"
        elif any(kw in summary for kw in ["流程", "步骤", "过程"]):
            return f"{time:.0f}s_投稿流程步骤"
        elif any(kw in summary for kw in ["案例", "例子", "实例", "举例"]):
            return f"{time:.0f}s_案例分析"
        elif any(kw in summary for kw in ["总结", "回顾", "最后"]):
            return f"{time:.0f}s_总结回顾"
        elif any(kw in summary for kw in ["系统", "架构", "框架", "设计"]):
            return f"{time:.0f}s_系统架构"
        else:
            return f"{time:.0f}s_{summary[:20].strip()}"

    def _dedup(self, targets: list, min_gap: float = 8) -> list:
        """去重，相邻时间太近的保留标签更短（更确定的）。"""
        if not targets: return []
        targets.sort(key=lambda x: x["time"])
        result = [targets[0]]
        for t in targets[1:]:
            if t["time"] - result[-1]["time"] >= min_gap:
                result.append(t)
            elif len(t["label"]) < len(result[-1]["label"]):
                result[-1] = t  # 用更确定的标签替换
        return result


# ═══════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════

async def main():
    # 清理
    today = time.strftime('%Y%m%d')
    for f in glob.glob(os.path.join(ROOT, f"captions_{today}*.txt")):
        try: os.remove(f)
        except: pass

    # 截图输出目录
    shots_dir = os.path.join(ROOT, "screenshots")
    os.makedirs(shots_dir, exist_ok=True)

    # ── 1. 耳朵 ──
    print("[1] 启动耳朵...")
    cap_proc = subprocess.Popen([sys.executable, CAPTION_SCRIPT], cwd=ROOT)
    cap_log = os.path.join(ROOT, f"captions_{time.strftime('%Y%m%d')}.txt")
    await asyncio.sleep(8)

    # ── 2. 手 ──
    print("[2] 启动手 (MCP)...")
    c = McpClient(SERVER_PATH); await c.start()
    t = T(c)

    try:
        # ── 3. 导航播放 ──
        print("[3] 导航...")
        await t.nav()
        await asyncio.sleep(5)
        st = await t.state()
        dur = st.get("duration",0)
        vt0 = st["videos"][0].get("currentTime",0) if st.get("videos") else 0

        caps_before = read_caps(cap_log, tail=3)
        cap_offset = caps_before[-1][0] if caps_before else 0
        print(f"  时长={dur:.0f}s | 当前={vt0:.1f}s | 字幕偏移={cap_offset:.1f}s")

        await t.play()
        play_start = time.time()

        # ── 4. 边播边记 ──
        check_times = [15, 35, 60, 90]
        elapsed = 0
        for ct in check_times:
            wait = ct - elapsed
            print(f"\n  [播放 {wait}s...]")
            await asyncio.sleep(wait)
            elapsed = ct
            await t.pause()
            await asyncio.sleep(2)

            st = await t.state()
            vt = st["videos"][0].get("currentTime",0) if st.get("videos") else 0
            print(f"  T_video={vt:.1f}s")

            caps = read_caps(cap_log, tail=5)
            if caps:
                for cct, txt in caps:
                    adj = cct - cap_offset
                    print(f"    [{adj:.0f}s] {txt[:80]}")

            if vt >= (dur or 999) * 0.85:
                break
            await t.play()

        # ── 5. LLM 分析 ──
        await t.pause()
        await asyncio.sleep(1)
        caps = read_caps(cap_log, tail=50)

        # 转换时间戳: 减去偏移 = 视频时间
        caps_vt = [(ct - cap_offset, txt) for ct, txt in caps if ct >= cap_offset]

        print(f"\n[5] LLM 收到 {len(caps_vt)} 行字幕:")
        for vt_t, txt in caps_vt:
            print(f"  [{vt_t:.0f}s] {txt[:90]}")

        llm = LLMSimulator()
        targets = await llm.analyze(caps_vt, dur)

        if not targets:
            print("  [LLM] 未找到值得截图的内容")
            return

        # ── 6. 执行工具调用 ──
        print("\n" + "="*60)
        print("  [Agent 执行] 按 LLM 决策 seek + 截图")
        print("="*60)

        for i, tgt in enumerate(targets):
            vt_target = tgt["time"]
            label = tgt["label"]
            reason = tgt["reason"]

            print(f"\n  [{i+1}/{len(targets)}] → {label}")
            print(f"    原因: {reason}")

            # 模拟工具调用网络延迟
            call_delay = random.uniform(0.1, 0.4)
            await asyncio.sleep(call_delay)
            print(f"    seek({vt_target:.0f})...")

            sr = await t.seek(vt_target)
            await asyncio.sleep(0.3)

            print(f"    screenshot...")
            scr = await t.scr()
            raw_path = scr.get("filepath","")

            # ── 按语义重命名截图 ──
            safe_label = re.sub(r'[<>:"/\\|?*]', '_', label)
            ext = os.path.splitext(raw_path)[1] if raw_path else ".png"
            new_name = f"{safe_label}{ext}"
            new_path = os.path.join(shots_dir, new_name)

            if raw_path and os.path.exists(raw_path):
                shutil.move(raw_path, new_path)
                print(f"    截图: {new_name}")

            # 找对应字幕
            nearby = [txt for vt_t, txt in caps_vt if abs(vt_t - vt_target) < 6]
            if nearby:
                print(f"    字幕: {nearby[0][:80]}")

        # ── 7. 总结 ──
        print("\n" + "="*60)
        print("  端到端 Agent 运行完成")
        print()
        print("  模拟了真实 LLM 工作流:")
        print(f"    1. 字幕积累 ({len(caps_vt)} 行)")
        print(f"    2. LLM 思考 (1-3s 延迟)")
        print(f"    3. 选中 {len(targets)} 个截图点 + 语义命名")
        print(f"    4. 每个点: seek → 截图 → 重命名")
        print()
        print("  截图文件:")
        for f in sorted(os.listdir(shots_dir)):
            if f.endswith(".png"):
                size_kb = os.path.getsize(os.path.join(shots_dir, f)) / 1024
                print(f"    {f} ({size_kb:.0f}KB)")
        print()
        print("  统一度量衡: 字幕时间戳 → LLM 理解 → seek → 截图")
        print("="*60)

    finally:
        cap_proc.terminate(); cap_proc.wait(timeout=5)
        await c.close()

if __name__=="__main__":
    asyncio.run(main())
