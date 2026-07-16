"""
Agent MCP Server — LLM 大脑 + 耳朵控制
- 启动/停止 Whisper 实时转录
- 读取字幕（带视频时间戳对齐）
- 分析字幕 → 返回截图决策
通过 stdio JSON-RPC 与宿主通信
"""
import sys, json, os, time, re, urllib.request, urllib.error, subprocess, glob

ROOT = os.path.dirname(os.path.abspath(__file__))
CAPTION_SCRIPT = os.path.join(ROOT, "live_caption_video.py")

# ── 配置 ──
LLM_API_URL = os.environ.get("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")  # 便宜模型即可
if not LLM_API_KEY:
    for p in [os.path.expanduser("~/.openai_key"), os.path.expanduser("~/.llm_key")]:
        if os.path.exists(p):
            LLM_API_KEY = open(p).read().strip(); break

# ── 全局状态: 字幕进程 ──
_cap_proc = None
_cap_log = ""
_cap_video_offset = 0.0  # live_caption 本地时间戳 → 视频时间的偏移

# ── LLM 约束 Prompt ──
SYSTEM_PROMPT = """你是一个视频内容分析 Agent。你的任务是根据 Whisper 转录的字幕文本，判断视频的哪些时间点值得截图。

## 输入格式
每行: `[T=XXs] 字幕内容`

## 你需要做的
1. 通读字幕，理解视频在讲什么主题
2. 识别以下类型的"关键帧"：
   - **标题/开场**: 视频标题页面、个人/频道介绍
   - **数据展示**: 提到了具体数字、对比、图表（如"影响因子2.5"、"审稿周期17天"）
   - **流程步骤**: 提到了步骤、顺序操作（如"第一步"、"然后点击"）
   - **架构/原理图**: 解释系统设计、架构、关系（如"ROS1用Master，ROS2用DDS"）
   - **代码/配置**: 展示代码或命令行
   - **案例/实战**: 实际操作演示
   - **总结/回顾**: 章节总结、要点汇总
3. **不要截图**纯口播闲聊、重复内容、没有视觉信息的时间段

## 输出格式
返回 JSON 数组，每个元素包含:
- `time`: 建议截图的视频时间（秒，整数）
- `label`: 中文语义标签（5-10字，如"期刊数据展示"、"投稿步骤说明"）
- `reason`: 一句话解释为什么这里值得截图（15字以内）

## 约束
- 每个视频最多返回 8 个截图点
- 相邻截图至少间隔 10 秒
- 只返回 JSON，不要其他文字
- 优先截取开头和关键转折点"""


def call_llm(transcript: str) -> list[dict]:
    """调用 LLM API，传入字幕，返回决策列表"""
    if not LLM_API_KEY:
        # 无 API key 时回退到规则模拟
        return _fallback_decide(transcript)

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"请分析以下视频字幕，返回值得截图的时间点：\n\n{transcript}"}
        ],
        "temperature": 0.3,
        "max_tokens": 800,
    }

    req = urllib.request.Request(
        LLM_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"].strip()
            # 提取 JSON
            s = content.find("[")
            e = content.rfind("]") + 1
            if s >= 0 and e > s:
                return json.loads(content[s:e])
            return json.loads(content)
    except Exception as ex:
        print(f"[Agent] LLM API 调用失败: {ex}", file=sys.stderr)
        return _fallback_decide(transcript)


def _read_caption_file(tail=30):
    """读 live_caption 日志文件，返回 [(本地时间戳秒, 文本), ...]"""
    global _cap_log
    log = _cap_log or os.path.join(ROOT, f"captions_{time.strftime('%Y%m%d')}.txt")
    if not os.path.exists(log):
        return []
    with open(log, "r", encoding="utf-8") as f:
        lines = f.readlines()
    caps = []
    for l in lines:
        # 匹配格式: [HH:MM:SS] text
        m = re.match(r"\[(\d{2}:\d{2}:\d{2})\]\s+(.+)", l)
        if m:
            h,m2,s = map(int, m.group(1).split(":"))
            local_ts = h * 3600 + m2 * 60 + s
            caps.append((float(local_ts), m.group(2)))
    return caps[-tail:] if len(caps) > tail else caps


def _fallback_decide(transcript: str) -> list[dict]:
    """无 API key 时的规则回退 —— 仅用于测试"""
    triggers = [
        (["大家好","欢迎","各位同学","哈喽","Hello","你好"], "开场介绍", "视频开场，应截取标题/讲者信息"),
        (["影响因子","自引率","版面费","审稿周期","年文章","预警"], "期刊数据展示", "提到具体数据指标"),
        (["第一步","第二步","步骤","流程","接下来","然后","首先"], "操作流程说明", "提到步骤或操作顺序"),
        (["案例","举例","实例","实战","演示","我们来看"], "案例分析", "提到案例或实际操作"),
        (["架构","系统","设计","框架","平台","接口"], "技术架构", "提到系统架构或设计"),
        (["总结","回顾","最后","结尾","以上就是"], "总结回顾", "章节或视频总结"),
        (["代码","命令","配置","参数","安装","pip","npm"], "代码/配置", "提到代码或命令行"),
    ]
    seen_labels = set()
    decisions = []
    for line in transcript.split("\n"):
        m = re.match(r"\[T=([\d.]+)s\]\s+(.+)", line)
        if not m: continue
        t = float(m.group(1)); txt = m.group(2)
        for keywords, label, reason in triggers:
            if label in seen_labels: continue
            if any(kw in txt for kw in keywords):
                # 间距检查
                if decisions and abs(decisions[-1]["time"] - t) < 10:
                    continue
                seen_labels.add(label)
                decisions.append({"time": int(t), "label": label, "reason": reason})
                break
        if len(decisions) >= 8:
            break
    return decisions


# ═══════════════════════════════════════════════════════════
#  MCP Server (stdio JSON-RPC)
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {
        "name": "caption_start",
        "description": """【耳朵】启动 Whisper 实时转录进程。
调用后 Whisper 开始监听扬声器并将转录结果写入 captions 文件。
每段字幕带本地时间戳 [HH:MM:SS]，Agent 需调用 caption_read 获取最新字幕。""",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "caption_read",
        "description": """【耳朵】读取 Whisper 最新转录的字幕文本，返回带视频时间戳的字幕。

参数:
- tail: 读取最近 N 行（默认 30）
- video_time: 当前视频播放时间（秒），用于对齐。传 0 表示只需本地时间戳。

返回:
- transcript: 格式 `[T=XXs] 字幕内容`（已对齐到视频时间）
- line_count: 返回的行数
- offset: 本地时间戳到视频时间的偏移量（秒）""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tail": {"type": "integer", "default": 30, "description": "读取最近 N 行"},
                "video_time": {"type": "number", "default": 0, "description": "当前视频播放时间（秒），用于对齐时间戳"},
            },
            "required": [],
        },
    },
    {
        "name": "caption_set_offset",
        "description": """【耳朵】设置 live_caption 本地时间戳与视频时间的对齐偏移。

参数:
- video_time: 当前视频 currentTime（秒）

内部计算: offset = live_caption当前最新本地时间戳 - video_time
之后 caption_read 输出的 [T=XXs] 会自动减去此偏移。""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_time": {"type": "number", "description": "当前视频 currentTime（秒）"},
            },
            "required": ["video_time"],
        },
    },
    {
        "name": "caption_stop",
        "description": """【耳朵】停止 Whisper 转录进程并返回最终字幕文本。""",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "agent_decide_screenshots",
        "description": """【Agent 大脑】分析 Whisper 转录的字幕文本，返回值得截图的时间点列表。

输入: 带视频时间戳的字幕文本，格式 `[T=XXs] 字幕内容`
输出: [{time: 秒, label: "语义标签", reason: "理由"}, ...]

LLM 会根据视频内容判断哪些帧有视觉价值（标题、数据、流程图、代码等），
跳过纯口播闲聊。每个视频最多返回 8 个截图点，相邻至少间隔 10 秒。""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcript": {
                    "type": "string",
                    "description": "带 [T=XXs] 时间戳的字幕文本，多行"
                },
            },
            "required": ["transcript"],
        },
    },
    {
        "name": "agent_summarize_chapter",
        "description": """【Agent 大脑】对一段字幕做章节级总结。

输入: 带时间戳的字幕文本片段
输出: {title: "章节标题", summary: "内容总结(50-100字)", start_time, end_time}""",
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcript": {
                    "type": "string",
                    "description": "带 [T=XXs] 时间戳的字幕文本片段"
                },
            },
            "required": ["transcript"],
        },
    },
]


def handle_request(req: dict) -> dict:
    """处理单个 JSON-RPC 请求"""
    global _cap_proc, _cap_log, _cap_video_offset
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-brain", "version": "1.0"},
            }
        }

    if method == "notifications/initialized":
        return None  # 通知不需要响应

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})

        # ── 耳朵: caption_start ──
        if tool_name == "caption_start":
            if _cap_proc and _cap_proc.poll() is None:
                return {"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps({"status":"already_running","pid":_cap_proc.pid})}]}}
            # 清理旧日志
            today = time.strftime("%Y%m%d")
            for f in glob.glob(os.path.join(ROOT, f"captions_{today}*.txt")):
                try: os.remove(f)
                except: pass
            # 启动
            _cap_proc = subprocess.Popen([sys.executable, CAPTION_SCRIPT], cwd=ROOT)
            _cap_log = os.path.join(ROOT, f"captions_{time.strftime('%Y%m%d')}.txt")
            return {"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps({"status":"started","pid":_cap_proc.pid,"log_file":_cap_log})}]}}

        # ── 耳朵: caption_read ──
        if tool_name == "caption_read":
            tail = args.get("tail", 30)
            video_time = args.get("video_time", 0)
            # 读日志文件
            caps = _read_caption_file(tail)
            if not caps:
                return {"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps({"transcript":"","line_count":0,"offset":_cap_video_offset})}]}}
            # 对齐到视频时间
            lines = []
            for local_ts, txt in caps:
                vt = local_ts - _cap_video_offset
                if vt >= 0:
                    lines.append(f"[T={vt:.1f}s] {txt}")
                else:
                    lines.append(f"[T=??s] {txt}")
            transcript = "\n".join(lines)
            return {"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps({"transcript":transcript,"line_count":len(lines),"offset":_cap_video_offset},ensure_ascii=False)}]}}

        # ── 耳朵: caption_set_offset ──
        if tool_name == "caption_set_offset":
            video_time = args["video_time"]
            caps = _read_caption_file(tail=3)
            if caps:
                latest_local = caps[-1][0]
                _cap_video_offset = latest_local - video_time
                return {"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps({"offset":_cap_video_offset,"latest_local":latest_local,"video_time":video_time})}]}}
            return {"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps({"offset":_cap_video_offset,"warning":"no captions yet"})}]}}

        # ── 耳朵: caption_stop ──
        if tool_name == "caption_stop":
            final_text = ""
            if _cap_proc and _cap_proc.poll() is None:
                _cap_proc.terminate()
                try: _cap_proc.wait(timeout=5)
                except: _cap_proc.kill()
            _cap_proc = None
            # 读最终字幕
            caps = _read_caption_file(tail=999)
            final_text = "\n".join([f"[T={ts - _cap_video_offset:.1f}s] {t}" for ts,t in caps if ts >= _cap_video_offset])
            return {"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps({"status":"stopped","transcript":final_text,"line_count":len(caps)},ensure_ascii=False)}]}}

        # ── 大脑: agent_decide_screenshots ──
            transcript = args.get("transcript", "")
            start = time.time()
            decisions = call_llm(transcript)
            elapsed = time.time() - start
            decisions_json = json.dumps(decisions, ensure_ascii=False)
            print(f"[Agent] 决策完成: {len(decisions)} 个截图点, 耗时 {elapsed:.1f}s", file=sys.stderr)
            return {
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "content": [
                        {"type": "text", "text": decisions_json}
                    ]
                }
            }

        if tool_name == "agent_summarize_chapter":
            transcript = args.get("transcript", "")
            # 简化版：用规则提取
            lines = [l for l in transcript.split("\n") if l.strip()]
            start_t = 0; end_t = 0
            m1 = re.search(r"T=([\d.]+)s", transcript)
            m2 = re.search(r"T=([\d.]+)s.*$", transcript)
            if m1: start_t = float(m1.group(1))
            if m2: end_t = float(m2.group(1))

            # 取前 200 字做摘要（实际应调 LLM，这里先简化）
            text = re.sub(r"\[T=[\d.]+s\]\s*", "", transcript)[:200]
            summary = {
                "title": f"片段 {start_t:.0f}s-{end_t:.0f}s",
                "summary": text + ("..." if len(text) >= 200 else ""),
                "start_time": start_t,
                "end_time": end_t,
            }
            return {
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(summary, ensure_ascii=False)}
                    ]
                }
            }

        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}

    # ping
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def main():
    """stdio JSON-RPC 主循环"""
    print("[Agent Brain] MCP Server 启动", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            continue
        except Exception as e:
            print(f"[Agent Brain] 错误: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    main()
