"""
DeepSeek API 客户端 — OpenAI 兼容接口。
提供截图决策、章节总结等 LLM 功能。
"""

from __future__ import annotations

import os
import json
import time
from typing import Any

import requests

# ── 配置 ────────────────────────────────────────────────

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"   # 最新模型，快且便宜
MAX_RETRIES = 3
REQUEST_TIMEOUT = 60  # 秒


# ── 视频分析提示词（v2：带状态 + 动作控制）─────────────────

ANALYZE_SCREENSHOTS_SYSTEM = """你是一个视频分析助手。每轮你会收到一段字幕文本和当前视频状态。

**视频状态字段**：
- video_time: 视频当前播放到的秒数
- duration: 视频总时长
- paused: 是否已暂停

**你每轮有 0~3 个截图额度**。你可以执行以下动作：

| 动作 | 说明 |
|------|------|
| seek(time) | 跳转到指定秒数 |
| pause() | 暂停视频（截图前必须暂停） |
| screenshot(name) | 截图，name 为简短中文描述（≤10字） |
| play() | 继续播放 |

**输出格式**（严格的 JSON，不要包含其他文字）：
{
  "summary": "这一段主要讲了...（≤50字）",
  "actions": [
    {"type": "seek", "time": 250.0},
    {"type": "pause"},
    {"type": "screenshot", "name": "架构图"},
    {"type": "play"}
  ]
}

**规则**：
- 每轮最多 3 个 screenshot
- 截图前必须先 seek + pause
- 截图后建议 play 恢复播放
- 没有值得截图的内容 → actions 为空数组 []
- 纯寒暄/闲聊/过渡话/重复 → 不截图
- 优先截：图表、公式、代码、架构、对比、关键概念
- 连续两次 screenshot 间隔 ≥ 30 秒"""


# ═══════════════════════════════════════════════════════════
#  DeepSeekClient
# ═══════════════════════════════════════════════════════════

class DeepSeekClient:
    """
    DeepSeek API 客户端。

    用法:
        client = DeepSeekClient(api_key="sk-xxx")
        # 或从环境变量 DEEPSEEK_API_KEY 自动读取

        shots = client.analyze_screenshots(subtitle_text)
        # → [{"time": 120.5, "name": "Transformer架构图"}, ...]
    """

    def __init__(self, api_key: str | None = None, base_url: str = DEEPSEEK_BASE_URL):
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._last_response: dict | None = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    # ── 底层 API ──

    def _call(
        self,
        messages: list[dict],
        model: str = DEEPSEEK_MODEL,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> str:
        """调用 DeepSeek Chat API，返回文本响应。"""
        url = f"{self._base_url}/chat/completions"

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._last_response = data
                    return data["choices"][0]["message"]["content"]
                elif resp.status_code == 429:
                    # 限流，等一会重试
                    wait = (attempt + 1) * 5
                    print(f"[DeepSeek] 限流，等待 {wait}s 后重试...")
                    time.sleep(wait)
                    last_error = f"429 Too Many Requests"
                elif resp.status_code == 401:
                    raise RuntimeError("DeepSeek API Key 无效，请检查 DEEPSEEK_API_KEY 环境变量")
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    print(f"[DeepSeek] API 错误: {last_error}")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(2)
            except requests.RequestException as e:
                last_error = str(e)
                print(f"[DeepSeek] 网络错误: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2)

        raise RuntimeError(f"DeepSeek API 调用失败（重试{MAX_RETRIES}次）: {last_error}")

    # ── 高层接口 ──

    def analyze_screenshots(
        self,
        subtitle_text: str,
        video_state: dict | None = None,
        custom_instruction: str | None = None,
    ) -> dict:
        """
        分析字幕 + 视频状态，返回截图动作决策。

        subtitle_text: 字幕文本（含 [T=XXs] 时间戳）
        video_state: {"video_time": 245.3, "duration": 1200, "paused": False}
        custom_instruction: 额外指令

        返回: {
            "summary": "分析摘要",
            "actions": [
                {"type": "seek", "time": 250.0},
                {"type": "pause"},
                {"type": "screenshot", "name": "架构图"},
                {"type": "play"},
            ]
        }
        """
        if not self._api_key:
            raise RuntimeError(
                "未配置 DeepSeek API Key。请设置环境变量 DEEPSEEK_API_KEY"
            )

        if not subtitle_text.strip():
            return {"summary": "", "actions": []}

        # 构建状态描述
        state_desc = ""
        if video_state:
            t = video_state.get("video_time", 0)
            d = video_state.get("duration", 0)
            p = "已暂停" if video_state.get("paused") else "播放中"
            state_desc = f"\n**当前视频状态**：{p}, 位置 {t:.0f}s / {d:.0f}s\n"

        user_prompt = f"请分析以下视频字幕，决定是否截图及如何操作：\n{state_desc}\n{subtitle_text}"

        if custom_instruction:
            user_prompt += f"\n\n额外要求：{custom_instruction}"

        messages = [
            {"role": "system", "content": ANALYZE_SCREENSHOTS_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        response = self._call(messages, temperature=0.3)

        # 解析 JSON
        try:
            parsed = json.loads(response.strip())
        except json.JSONDecodeError:
            import re
            match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response)
            if match:
                try:
                    parsed = json.loads(match.group(1).strip())
                except json.JSONDecodeError:
                    parsed = self._repair_action_json(response)
            else:
                parsed = self._repair_action_json(response)

        if not isinstance(parsed, dict):
            raise RuntimeError(f"DeepSeek 返回格式错误，期望对象: {response[:200]}")

        # 验证 actions
        valid_actions = []
        screenshot_count = 0
        for act in parsed.get("actions", []):
            if not isinstance(act, dict):
                continue
            t = act.get("type", "")
            if t == "screenshot":
                if screenshot_count >= 3:
                    continue  # 超出额度
                name = act.get("name", "").strip()[:20]
                if name:
                    valid_actions.append({"type": "screenshot", "name": name})
                    screenshot_count += 1
            elif t in ("seek", "pause", "play"):
                a = {"type": t}
                if t == "seek" and "time" in act:
                    a["time"] = float(act["time"])
                valid_actions.append(a)

        return {
            "summary": str(parsed.get("summary", ""))[:100],
            "actions": valid_actions,
        }

    # ── 通用聊天（支持动作指令）──

    def chat(
        self,
        user_message: str,
        video_state: dict | None = None,
        conversation_history: list[dict] | None = None,
    ) -> dict:
        """
        通用聊天接口。DS 可以回复文字，也可以附带动作指令。

        返回: {
            "reply": "好的，我来帮你...",
            "actions": [  # 可选，DS 需要执行的操作
                {"type": "navigate", "url": "https://..."},
                {"type": "seek", "time": 120.0},
                {"type": "pause"},
                {"type": "play"},
                {"type": "screenshot", "name": "描述"},
                {"type": "analyze"},          # 启动自动分析
                {"type": "status"},           # 查看视频状态
                {"type": "search", "query": "CNN教程"},
            ]
        }
        """
        if not self._api_key:
            return {"reply": "未配置 DeepSeek API Key，请在设置中填入密钥。", "actions": []}

        # 构建系统提示
        state_desc = ""
        if video_state:
            pl = video_state.get("page_title", "未知")
            pu = video_state.get("page_url", "")
            hv = video_state.get("has_video", False)
            t = video_state.get("video_time", 0)
            d = video_state.get("duration", 0)
            p = "已暂停" if video_state.get("paused") else "播放中" if hv else "无视频"
            state_desc = (
                f"\n**当前页面**：\n"
                f"- 标题: {pl}\n"
                f"- URL: {pu}\n"
                f"- 视频: {'有' if hv else '无'}\n"
                f"- 进度: {t:.0f}s / {d:.0f}s  |  {p}\n"
            )

        system_prompt = CHAT_SYSTEM_PROMPT + state_desc

        messages = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history[-20:])  # 保留最近20条
        messages.append({"role": "user", "content": user_message})

        response = self._call(messages, temperature=0.7, max_tokens=2048)

        # 解析 response
        parsed = self._parse_chat_response(response)

        return parsed

    def _parse_chat_response(self, response: str) -> dict:
        """解析 DS 的聊天响应，提取文字和动作。"""
        import re

        result = {"reply": response.strip(), "actions": []}

        # 尝试提取 JSON 动作块
        json_match = re.search(r'```json\s*([\s\S]*?)```', response)
        if not json_match:
            json_match = re.search(r'\{[\s\S]*"actions"[\s\S]*\}', response)

        if json_match:
            json_str = json_match.group(1) if json_match.lastindex else json_match.group(0)
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, dict):
                    result["reply"] = parsed.get("reply", result["reply"])
                    raw_actions = parsed.get("actions", [])
                    if isinstance(raw_actions, list):
                        result["actions"] = [a for a in raw_actions if isinstance(a, dict)]
                    # 去掉 JSON 块，只保留纯文本回复
                    result["reply"] = result["reply"].strip()
            except json.JSONDecodeError:
                pass

        return result

    def _repair_action_json(self, text: str) -> dict:
        """尝试修复残缺的 JSON。"""
        import re

        # 提取 summary
        summary = ""
        sm = re.search(r'"summary"\s*:\s*"([^"]*)"', text)
        if sm:
            summary = sm.group(1)

        # 提取 actions 数组
        actions = []
        # 匹配 {"type": "xxx", ...} 模式
        action_pattern = re.findall(
            r'\{\s*"type"\s*:\s*"(\w+)"[^}]*\}', text
        )
        for atype in action_pattern:
            if atype in ("seek", "pause", "play", "screenshot", "click", "navigate",
                         "analyze", "status", "search", "get_page", "new_session"):
                a = {"type": atype}
                if atype == "seek":
                    tm = re.search(r'"time"\s*:\s*([\d.]+)', text)
                    if tm:
                        a["time"] = float(tm.group(1))
                if atype == "screenshot":
                    nm = re.search(r'"name"\s*:\s*"([^"]*)"', text)
                    if nm:
                        a["name"] = nm.group(1)[:20]
                if atype in ("navigate", "search"):
                    um = re.search(r'"(?:url|query)"\s*:\s*"([^"]*)"', text)
                    if um:
                        key = "url" if atype == "navigate" else "query"
                        a[key] = um.group(1)
                if atype == "click":
                    tm = re.search(r'"text"\s*:\s*"([^"]*)"', text)
                    if tm:
                        a["text"] = tm.group(1)[:60]
                    im = re.search(r'"index"\s*:\s*(\d+)', text)
                    if im:
                        a["index"] = int(im.group(1))
                actions.append(a)

        return {"summary": summary, "actions": actions[:3]}


# ── 通用聊天系统提示 ─────────────────────────────────────

CHAT_SYSTEM_PROMPT = """你是 VideoAgent，一个能直接操控浏览器的 AI 助手。你有眼睛（能看到当前页面内容），也有手（能导航/搜索/截图/播放视频）。

**⚠ 关键规则：永远不要向用户说你在用什么工具、调用什么接口、系统支不支持。
用户不需要知道你的内部机制。你能做到就直接做，做不到就如实说结果，不要说「我需要调用XX」「系统不支持XX」「我无法模拟点击」。**

**当前页面内容会直接附在用户消息后面**，你能看到页面上的链接、按钮、标题。根据用户的需求和你看到的页面内容，自然做出反应。

**你的回复格式**（混合文字+JSON）：
先自然语言回复用户（像真人一样），**只在需要操作浏览器时**，最后附加一个 JSON 块:

```json
{
  "reply": "看到了，这是ROS入门21讲的课程主页，一共21个视频...",
  "actions": [
    {"type": "analyze"}
  ]
}
```

**支持的动作**：
- navigate(url) — 导航到网址
- seek(time) — 跳转到指定秒数
- pause() / play() — 暂停/播放
- screenshot(name) — 截图
- click(text, index) — 点击页面上文本为 text 的元素（第 index 个匹配项，默认0）
- analyze — 在视频页启动字幕+分析
- search(query) — 搜索视频
- get_page — 需要更详细的页面元素时（链接、按钮等），系统会回传并让你二次决策
- new_session — 切换视频时新建会话文件夹

**click 用法示例**：
- 用户说「打开第十讲」→ 在页面元素中看到「第10讲·通信接口」→ click(text="第10讲·通信接口")
- 列表页有多个「立即学习」按钮 → click(text="立即学习", index=2) 表示第3个
- 不需要用 get_page → 你已经在消息里看到了页面元素，文本是「第10讲·通信接口」就直接 click

**规则**：
- 纯聊天不需要 JSON，直接文字回复
- 用户问「这是什么页面」「看得到吗」「有哪些链接」→ 你已在消息开头收到了页面内容，直接回答
- 回复简洁自然，像真人助理，不要像机器人
- 需要操作浏览器时才附加 JSON
- analyze 命令会启动完整字幕+定时截图分析流程
- 用户在列表页/搜索结果页 → 用 get_page 查看完整页面结构，找到匹配链接后 navigate + analyze
- 用户要搜视频 → search(query)，找到后 navigate + analyze"""


