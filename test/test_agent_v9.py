"""
Agent v9 — 完整回路: 耳朵实时转录 + LLM决策 + video_capture_batch 批量截图
"""
import asyncio, json, os, sys, subprocess, time, re, glob, random, threading

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(ROOT, "dist", "index.js")
CAPTION_SCRIPT = os.path.join(ROOT, "live_caption_video.py")
URL = "https://www.bilibili.com/video/BV1mxb3zKEzL/?spm_id_from=333.337.search-card.all.click&vd_source=65647d080500ee5a6f027fa53c67a0de"

def read_caps(path, tail=30):
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f: lines=f.readlines()
    caps=[]
    for l in lines:
        m=re.match(r"\[T=([\d.]+)s\]\s+(.+)",l)
        if m: caps.append((float(m.group(1)),m.group(2)))
    return caps[-tail:]


class VideoMCP:
    """通过 stdio 控制视频 MCP Server"""
    def __init__(self):
        self.proc = subprocess.Popen(
            ["node", SERVER_PATH], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._id = 0
        self._pending = {}
    def send(self, method, params=None):
        self._id += 1
        msg = json.dumps({"jsonrpc":"2.0","method":method,"params":params or {},"id":self._id})
        self.proc.stdin.write((msg+"\n").encode()); self.proc.stdin.flush()
        self._pending[self._id] = method.replace("tools/call", params.get("name","") if params else "")
        return self._id
    def recv(self, timeout=120):
        start = time.time()
        buf = b""
        while time.time() - start < timeout:
            raw = self.proc.stdout.readline()
            if not raw:
                if self.proc.poll() is not None: return None
                time.sleep(0.1); continue
            buf += raw
            try:
                r = json.loads(buf.decode())
                return r
            except: continue
        return None
    def call(self, tool, args=None, timeout=120):
        prev = self._id
        self._id += 1
        msg = json.dumps({"jsonrpc":"2.0","method":"tools/call","params":{"name":tool,"arguments":args or {}},"id":self._id})
        self.proc.stdin.write((msg+"\n").encode()); self.proc.stdin.flush()
        self._pending[self._id] = tool
        # 只读到此 id 的响应
        start = time.time()
        result = None
        while time.time() - start < timeout:
            raw = self.proc.stdout.readline()
            if not raw:
                if self.proc.poll() is not None: return None
                time.sleep(0.1); continue
            try:
                r = json.loads(raw.decode())
                if r.get("id") == self._id:
                    result = r
                    break
            except: pass
        if result and result.get("result"):
            contents = result["result"].get("content", [])
            for c in contents:
                if c["type"] == "text":
                    return json.loads(c["text"])
        return result
    def close(self):
        try: self.proc.terminate(); self.proc.wait(timeout=5)
        except: self.proc.kill()


class AgentBrain:
    """LLM 决策模拟器（规则回退）"""
    def __init__(self):
        self.triggers = [
            (["大家好","欢迎","各位同学","哈喽","Hello"], "开场介绍"),
            (["期刊","投稿","MDPI"], "期刊背景"),
            (["流程","步骤","第一步","第二步","注册","登录"], "投稿流程"),
            (["案例","实例","举例","实战"], "实战案例"),
            (["总结","回顾","最后","结尾"], "总结回顾"),
            (["影响因子","自引率","预警","版面费","四区","开源"], "期刊数据"),
        ]
        self.seen = set()
        self.processed = []
    def is_processed(self, vt):
        return any(s <= vt <= e for s,e in self.processed)
    def mark(self, vt, window=20):
        half = window/2
        self.processed.append((max(0, vt-half), vt+half))
    def decide(self, caps_vt, current_video_time):
        """从字幕中提取截图点"""
        shots = []
        for vt, txt in caps_vt:
            if self.is_processed(vt): continue
            for keywords, label in self.triggers:
                if label in self.seen: continue
                if any(kw in txt for kw in keywords):
                    self.seen.add(label)
                    self.mark(vt)
                    # 找到最靠近的视频时间
                    shots.append({"time": int(vt), "name": label})
                    break
        return shots


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════
def main():
    print("="*60)
    print("Agent v9 — 耳朵实时监听 + 大脑决策 + video_capture_batch")
    print("="*60)

    # 1. 清理旧字幕日志
    today = time.strftime("%Y%m%d")
    for f in glob.glob(os.path.join(ROOT, f"captions_{today}*.txt")):
        os.remove(f); print(f"  [清理] {f}")

    # 2. 启动耳朵（Whisper 实时转录）
    print("\n[耳朵] 启动 Whisper 实时转录...")
    ear = subprocess.Popen([sys.executable, CAPTION_SCRIPT], cwd=ROOT)
    cap_file = glob.glob(os.path.join(ROOT, f"captions_{today}*.txt"))
    if not cap_file:
        # 给一点时间让它创建文件
        for _ in range(20):
            time.sleep(1)
            cap_file = glob.glob(os.path.join(ROOT, f"captions_{today}*.txt"))
            if cap_file: break
    cap_file = cap_file[0] if cap_file else os.path.join(ROOT, f"captions_{today}.txt")
    print(f"  PID={ear.pid}, 日志: {cap_file}")

    # 3. 启动视频 MCP
    print("\n[手] 启动视频 MCP Server...")
    mcp = VideoMCP()
    mcp.send("initialize", {"protocolVersion":"2025-06-18","clientInfo":{"name":"test","version":"1.0"},"capabilities":{}})
    r = mcp.recv()
    print(f"  initialize: {'OK' if r else 'FAIL'}")

    # 4. 导航到视频
    print(f"\n[手] 导航到视频...")
    r = mcp.call("video_navigate", {"url":URL, "waitUntil":"networkidle", "timeout":120000})
    print(f"  hasVideo={r.get('hasVideo')}, duration={r['videos'][0].get('duration') if r.get('videos') else 'N/A'}s")

    # 5. 设置对齐偏移
    print("\n[对齐] 设置时间戳偏移...")
    r = mcp.call("video_get_state")
    video_time = r["videos"][0]["currentTime"]

    # 等待耳朵产生一些字幕
    print("  等待耳朵转录（5秒）...")
    time.sleep(5)
    caps = read_caps(cap_file, tail=5)
    if caps:
        last_local = caps[-1][0]  # live_caption 的本地时间戳
        offset = last_local - video_time
        print(f"  最新本地时间戳={last_local:.1f}s, 视频时间={video_time:.1f}s")
        print(f"  偏移={offset:.1f}s (耳朵比视频早{offset:.1f}s)")
    else:
        offset = 0
        print("  警告: 耳朵还没产生字幕")

    # 6. 播放视频
    print("\n[手] 播放视频...")
    r = mcp.call("video_play")
    print(f"  {r.get('method','?')} playing={r.get('playing')}")

    brain = AgentBrain()
    all_shots = []

    # 7. 主循环: 播放 → 读字幕 → 决策 → 积累截图点
    print("\n" + "="*60)
    print("[循环] 播放中... 每 10 秒检测一次字幕")
    print("="*60)
    
    rounds = 0
    max_rounds = 8
    while rounds < max_rounds:
        time.sleep(8)
        rounds += 1

        # 读当前视频时间
        r = mcp.call("video_get_state")
        vt_now = r["videos"][0]["currentTime"]

        # 读字幕（对齐到视频时间）
        caps_vt = []
        raw = read_caps(cap_file, tail=40)
        for local_ts, txt in raw:
            v_ts = local_ts - offset
            if v_ts >= 0:
                caps_vt.append((v_ts, txt))

        if not caps_vt:
            print(f"[Round {rounds}] 视频={vt_now:.1f}s 暂无新字幕, 继续...")
            continue

        print(f"[Round {rounds}] 视频={vt_now:.1f}s, 字幕={caps_vt[0][1][:30]}...")

        # LLM 决策
        new_shots = brain.decide(caps_vt, vt_now)
        if new_shots:
            for s in new_shots:
                print(f"  → 发现截图点: {s['name']} @ {s['time']}s")
                all_shots.append(s)

        # 如果积够了或检测完毕，跳出
        if len(all_shots) >= 6 or vt_now > 180:
            break

    # 8. 批量截图
    print("\n" + "="*60)
    print(f"[截图] video_capture_batch: {len(all_shots)} 个目标")
    for s in all_shots:
        print(f"  {s['time']}s → {s['name']}")
    print("="*60)

    if all_shots:
        r = mcp.call("video_capture_batch", {
            "shots": all_shots,
            "tolerance": 0.5,
            "maxWaitMs": 8000,
        })
        if r:
            print(f"\n[结果] total={r.get('totalShots')}, captured={r.get('captured')}, failed={r.get('failed')}")
            print(f"  保存位置: {r.get('savedPosition'):.1f}s" if r.get('savedPosition') else f"  保存位置: {r.get('savedPosition')}")
            print(f"  回位: {r.get('returnedTo'):.1f}s, drift={r.get('returnDrift'):.2f}s" if r.get('returnDrift') is not None else f"  回位: {r.get('returnedTo')}")
            if r.get('resumePlay'):
                print(f"  ✓ 已恢复播放")
            for shot in r.get("results", []):
                if shot.get("error"):
                    print(f"    ✗ {shot['name']}: {shot['error']}")
                else:
                    status = "✓" if shot.get("reliable") else "⚠"
                    print(f"    {status} {shot['name']} @ {shot['targetTime']}s → {shot.get('filepath','?')} (drift={shot.get('drift',0):.2f}s)")
        else:
            print("  [截图] 超时/失败")

    # 9. 验证回位
    print("\n[验证] 检查回位...")
    r = mcp.call("video_get_state")
    print(f"  currentTime={r['videos'][0]['currentTime']:.1f}s, paused={r['videos'][0]['paused']}")

    # 10. 清理
    print("\n[清理] 停止耳朵...")
    ear.terminate()
    try: ear.wait(timeout=5)
    except: ear.kill()
    mcp.close()
    print("[完成] 全链路测试结束")

if __name__ == "__main__":
    main()
