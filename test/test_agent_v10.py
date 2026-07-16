"""
Agent v10 — 5秒日志 + 完整回路
"""
import json, os, sys, subprocess, time, re, glob

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_PATH = os.path.join(ROOT, "dist", "index.js")
CAPTION_SCRIPT = os.path.join(ROOT, "live_caption_video.py")
URL = "https://www.bilibili.com/video/BV1mxb3zKEzL/?spm_id_from=333.337.search-card.all.click&vd_source=65647d080500ee5a6f027fa53c67a0de"

def read_caps(path, tail=30):
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f: lines=f.readlines()
    caps=[]
    for l in lines:
        m=re.match(r"\[T=([\d.]+)s\]\s+(.+)" if "T=" in l else r"\[(\d{2}:\d{2}:\d{2})\]\s+(.+)", l)
        if m: caps.append((float(m.group(1)),m.group(2)))
    return caps[-tail:]


class VideoMCP:
    def __init__(self):
        self.proc = subprocess.Popen(
            ["node", SERVER_PATH], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self._id = 0
    def _call(self, method, params=None, timeout=120):
        self._id += 1
        msg = json.dumps({"jsonrpc":"2.0","method":method,"params":params or {},"id":self._id})
        self.proc.stdin.write((msg+"\n").encode()); self.proc.stdin.flush()
        start = time.time()
        result = None
        while time.time() - start < timeout:
            raw = self.proc.stdout.readline()
            if not raw: time.sleep(0.1); continue
            try:
                r = json.loads(raw.decode())
                if r.get("id") == self._id: result = r; break
            except: pass
        if result and result.get("result"):
            for c in result["result"].get("content", []):
                if c["type"] == "text": return json.loads(c["text"])
        return result
    def tool(self, name, args=None, timeout=120):
        return self._call("tools/call", {"name":name,"arguments":args or {}}, timeout)
    def close(self):
        try: self.proc.terminate(); self.proc.wait(timeout=5)
        except: self.proc.kill()


def main():
    print("="*60)
    print("Agent v10 — 5秒日志 · 完整回路")
    print("="*60)

    # 清理
    today = time.strftime("%Y%m%d")
    for f in glob.glob(os.path.join(ROOT, f"captions_{today}*.txt")):
        os.remove(f)

    # 启动耳朵（重定向输出避免 GUI 污染）
    print("\n[耳朵] 启动 Whisper...")
    ear = subprocess.Popen(
        [sys.executable, CAPTION_SCRIPT], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    # 等文件创建
    for _ in range(30):
        time.sleep(1)
        cap_file = glob.glob(os.path.join(ROOT, f"captions_{today}*.txt"))
        if cap_file: break
    cap_file = cap_file[0] if cap_file else os.path.join(ROOT, f"captions_{today}.txt")
    print(f"  PID={ear.pid}, 日志={os.path.basename(cap_file)}")

    # 初始化 MCP
    print("[手] 启动视频 MCP...")
    mcp = VideoMCP()
    mcp._call("initialize", {"protocolVersion":"2025-06-18","clientInfo":{"name":"test","version":"1.0"},"capabilities":{}})

    # 导航
    print("[手] 导航...")
    r = mcp.tool("video_navigate", {"url":URL, "waitUntil":"networkidle", "timeout":120000})
    dur = r["videos"][0]["duration"] if r.get("videos") else 0
    print(f"  {dur}s 视频就绪")

    # 对齐
    r = mcp.tool("video_get_state")
    vt0 = r["videos"][0]["currentTime"]
    time.sleep(3)
    caps = read_caps(cap_file, 5)
    offset = caps[-1][0] - vt0 if caps else 0
    print(f"[对齐] offset={offset:.1f}s")

    # 播放
    print("[手] 播放...")
    mcp.tool("video_play")

    # ═══════════════════════════════════════
    # 5秒循环日志
    # ═══════════════════════════════════════
    triggers = {
        "开场介绍": ["大家好","欢迎","各位同学"],
        "期刊背景": ["期刊","投稿","MDPI"],
        "期刊数据": ["影响因子","自引率","版面费","四区"],
        "投稿流程": ["流程","步骤","第一步","注册","登录"],
        "实战案例": ["案例","实例","举例","实战","演示"],
        "总结回顾": ["总结","回顾","最后","结尾"],
    }
    seen = set()
    shots = []
    processed = []

    print("\n" + "="*60)
    print("每5秒打印日志 | 按 Ctrl+C 停止")
    print("="*60)

    try:
        for tick in range(24):  # 最多跑 2 分钟
            time.sleep(5)
            r = mcp.tool("video_get_state")
            vt = r["videos"][0]["currentTime"]
            caps_vt = [(ts - offset, txt) for ts, txt in read_caps(cap_file, 50) if ts >= offset]

            # 决策
            new_points = []
            for vts, txt in caps_vt:
                if any(s <= vts <= e for s,e in processed): continue
                for label, kws in triggers.items():
                    if label in seen: continue
                    if any(kw in txt for kw in kws):
                        seen.add(label)
                        new_points.append((vts, label))
                        processed.append((max(0, vts-10), vts+10))
                        break

            print(f"[{vt:6.1f}s] 字幕{caps_vt[-1][0]:5.1f}s「{caps_vt[-1][1][:30]}...」", end="")
            if new_points:
                for p, lbl in new_points:
                    print(f"  → 发现 {lbl}@{p:.0f}s")
                    shots.append({"time": int(p), "name": lbl})
            else:
                print()

            if vt > 180 or len(shots) >= 6: break

    except KeyboardInterrupt:
        print("\n中断")

    # ═══════════════════════════════════════
    # 批量截图
    # ═══════════════════════════════════════
    print("\n" + "="*60)
    print(f"[截图] video_capture_batch: {len(shots)} 个目标")
    for s in shots: print(f"  {s['time']}s → {s['name']}")

    if shots:
        r = mcp.tool("video_capture_batch", {"shots": shots, "maxWaitMs": 8000}, timeout=120)
        if r:
            print(f"\n[结果] {r.get('captured')}/{r.get('totalShots')} 成功, {r.get('failed')} 失败")
            print(f"  保存位置: {r['savedPosition']:.1f}s → 回位: {r['returnedTo']:.1f}s, drift={r['returnDrift']:.2f}s")
            print(f"  恢复播放: {'是' if r.get('resumePlay') else '否'}")
            for s in r.get("results", []):
                if s.get("error"): print(f"    x {s['name']}: {s['error']}")
                else: print(f"    {'v' if s.get('reliable') else '!'} {s['name']}@{s['targetTime']}s → {s.get('filepath')} (drift={s.get('drift',0):.2f})")

    # 验证回位
    r = mcp.tool("video_get_state")
    print(f"\n[验证] 回位后 video.time={r['videos'][0]['currentTime']:.1f}s, paused={r['videos'][0]['paused']}")

    ear.terminate()
    try: ear.wait(timeout=5)
    except: ear.kill()
    mcp.close()
    print("[完成]")

if __name__ == "__main__":
    main()
