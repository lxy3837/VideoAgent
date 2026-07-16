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

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-v4-flash"   # 最新模型，快且便宜
MAX_RETRIES = 3
REQUEST_TIMEOUT = 60  # 秒


# ── 截图决策提示词 ──────────────────────────────────────

ANALYZE_SCREENSHOTS_SYSTEM = """你是一个视频内容分析专家。用户会给你一段视频字幕文本（带时间戳），你需要：
1. 阅读字幕内容，理解视频讲述的核心内容
2. 找出最值得截图的关键时刻（如：架构图、公式、代码演示、关键概念、数据图表、对比表格等）
3. 为每个截图时间点生成一个简短的中文描述名称（10字以内）

输出格式必须是严格的 JSON 数组，不要包含任何其他文字：
[{"time": 秒数, "name": "简短名称"}, ...]

规则：
- 至少输出 3 个时间点，最多 10 个
- time 使用字幕中 [T=XXs] 的时间戳
- name 要能一眼看出截图内容是什么
- 优先选择有视觉内容输出的时刻（图示、公式、代码、对比等）
- 纯口语过渡、寒暄、重复内容不截图
- 不要让连续两个截图的间隔小于 30 秒"""


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
        custom_instruction: str | None = None,
    ) -> list[dict]:
        """
        分析字幕文本，返回截图决策。

        subtitle_text: 字幕文本（含 [T=XXs] 时间戳）
        custom_instruction: 额外指令（如"多截一些代码相关的图"）

        返回: [{"time": 120.5, "name": "架构图"}, ...]
        """
        if not self._api_key:
            raise RuntimeError(
                "未配置 DeepSeek API Key。请设置环境变量 DEEPSEEK_API_KEY"
            )

        if not subtitle_text.strip():
            return []

        user_prompt = f"请分析以下视频字幕，找出值得截图的关键时刻：\n\n{subtitle_text}"

        if custom_instruction:
            user_prompt += f"\n\n额外要求：{custom_instruction}"

        messages = [
            {"role": "system", "content": ANALYZE_SCREENSHOTS_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        response = self._call(messages, temperature=0.3)

        # 解析 JSON
        try:
            # 先尝试直接解析
            shots = json.loads(response.strip())
        except json.JSONDecodeError:
            # 可能有 markdown 包裹 ```json ... ```
            import re
            match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response)
            if match:
                try:
                    shots = json.loads(match.group(1).strip())
                except json.JSONDecodeError:
                    # 尝试修复截断的 JSON
                    shots = self._repair_truncated_json(response)
            else:
                shots = self._repair_truncated_json(response)

        # 验证格式
        if not isinstance(shots, list):
            raise RuntimeError(f"DeepSeek 返回格式错误，期望数组: {response[:200]}")

        validated = []
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            t = shot.get("time")
            name = shot.get("name", "")
            if t is None:
                continue
            validated.append({
                "time": float(t),
                "name": str(name).strip(),
            })

        if not validated:
            print("[DeepSeek] 警告：未生成任何截图决策")

        return validated

    def summarize_chapter(
        self,
        full_subtitle: str,
        video_title: str = "",
    ) -> dict:
        """
        对整个视频字幕做章节总结（后续实现）。

        返回: {"title": "...", "summary": "...", "segments": [...]}
        """
        # TODO: 后续实现
        return {
            "title": video_title,
            "summary": "",
            "segments": [],
        }

    @staticmethod
    def _repair_truncated_json(text: str) -> list:
        """尝试修复不完整/截断的 JSON 数组。"""
        import re
        # 提取所有 {...} 对象
        pattern = r'\{\s*"time"\s*:\s*([\d.]+)\s*,\s*"name"\s*:\s*"([^"]*)"\s*\}'
        matches = re.findall(pattern, text)
        if matches:
            return [{"time": float(t), "name": n} for t, n in matches]

        # 兜底：找任何 {time: ..., name: ...} 格式
        pattern2 = r'"time"\s*:\s*([\d.]+).*?"name"\s*:\s*"([^"]*)"'
        matches2 = re.findall(pattern2, text)
        if matches2:
            return [{"time": float(t), "name": n} for t, n in matches2]

        return []
