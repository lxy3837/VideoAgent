"""
MCP 客户端 — 管理 MCP Server 子进程，提供简洁的 Python API。

用法:
  client = MCPClient()
  client.start()                    # 启动 mcp_server.py 子进程
  result = client.call("session_list")   # 调用 MCP 工具
  result = client.call("browser_click", {"text": "立即学习"})
  client.stop()
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any


ROOT = os.path.dirname(os.path.abspath(__file__))


class MCPClient:
    """VideoAgent MCP 客户端。

    启动 mcp_server.py 作为子进程，通过 stdio JSON-RPC 通信。
    """

    def __init__(self, log_callback=None):
        self._proc: "subprocess.Popen | None" = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._log = log_callback or (lambda msg, level: None)
        self._results: dict[int, dict] = {}  # req_id → result
        self._ready = threading.Event()
        self._reader_thread: "threading.Thread | None" = None
        self._tools: list[dict] = []  # 缓存的工具列表

    @property
    def tools(self) -> list[dict]:
        """获取 MCP 工具列表（用于注入 DS system prompt）。"""
        return self._tools

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── 生命周期 ──

    def start(self) -> bool:
        """启动 MCP Server 子进程并完成握手机制。"""
        if self.running:
            return True

        server_path = os.path.join(ROOT, "mcp_server.py")
        if not os.path.exists(server_path):
            self._log(f"MCP Server 不存在: {server_path}", "error")
            return False

        try:
            self._proc = subprocess.Popen(
                [sys.executable, server_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",  # 强制 UTF-8，与服务端 sys.stdout.reconfigure 一致
                bufsize=1,
                cwd=ROOT,
            )
        except Exception as e:
            self._log(f"启动 MCP Server 失败: {e}", "error")
            return False

        # 启动 stderr 读取线程（防止 pipe 满导致进程卡死）
        threading.Thread(target=self._read_stderr, daemon=True).start()

        # 启动 stdout 读取线程
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        # 握手机制（带重试，因为 mcp_server 启动可能因环境差异而较慢）
        max_retries = 3
        init_timeout = 15  # 首次尝试 15s（延迟导入后启动应很快）
        for attempt in range(max_retries):
            # 先确认子进程还活着
            if self._proc.poll() is not None:
                self._log(f"MCP Server 进程已退出 (code={self._proc.returncode})", "error")
                self.stop()
                return False

            init_result = self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "videoagent", "version": "2.3"},
            }, timeout=init_timeout)
            if init_result:
                break  # 握手成功

            if attempt < max_retries - 1:
                # 逐次放宽超时: 15s → 30s → 45s
                init_timeout = 15 + (attempt + 1) * 15
                self._log(f"MCP 握手重试 {attempt + 1}/{max_retries - 1} (timeout={init_timeout}s)...", "warn")
                time.sleep(1)
            else:
                self._log("MCP 握手失败（已重试多次）", "error")
                self.stop()
                return False

        # 发送 initialized 通知
        self._send_notification("notifications/initialized", {})

        # 获取工具列表
        tools_result = self._send_request("tools/list", {})
        if tools_result and "tools" in tools_result:
            self._tools = tools_result["tools"]
            self._log(f"MCP Server 就绪，{len(self._tools)} 个工具", "success")
        else:
            self._log("获取工具列表失败", "warn")

        return True

    def stop(self):
        """停止 MCP Server。"""
        if self._proc:
            try:
                self._proc.stdin.close()
                self._proc.stdout.close()
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self._tools = []
        self._ready.clear()

    # ── 工具调用 ──

    def call(self, tool_name: str, args: dict | None = None, timeout: float = 30) -> dict:
        """同步调用 MCP 工具，返回解析后的结果字典。

        Args:
            tool_name: 工具名称（如 "session_create"）
            args:      工具参数（dict）
            timeout:   超时秒数（默认 30，browser_connect 等长时间工具需要更大值）

        Returns:
            工具返回的 dict（已从 JSON 解析）
        """
        if not self.running:
            return {"error": "MCP Server 未运行"}

        params = {
            "name": tool_name,
            "arguments": args or {},
        }
        try:
            raw = self._send_request("tools/call", params, timeout=timeout)
            if not raw:
                return {"error": f"MCP 调用超时 ({timeout:.0f}s)"}
            # MCP 协议: result.content[0].text 是 JSON 字符串
            content = raw.get("content", [])
            if content and isinstance(content, list):
                text = content[0].get("text", "{}")
                return json.loads(text)
            return raw
        except json.JSONDecodeError:
            return {"error": "MCP 响应解析失败"}
        except Exception as e:
            return {"error": str(e)}

    # ── 内部 JSON-RPC ──

    def _send_request(self, method: str, params: dict, timeout: float = 15) -> dict | None:
        """发送 JSON-RPC 请求，等待并返回 result。"""
        with self._lock:
            self._req_id += 1
            req_id = self._req_id

        req = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        try:
            self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            self._log(f"MCP 写入失败: {e}", "error")
            return None

        # 等待结果
        deadline = time.time() + timeout
        while time.time() < deadline:
            if req_id in self._results:
                result = self._results.pop(req_id)
                if "error" in result:
                    self._log(f"MCP 错误: {result['error']}", "warn")
                    return None
                return result.get("result")
            time.sleep(0.05)

        self._log(f"MCP 调用超时: {method}", "warn")
        return None

    def _send_notification(self, method: str, params: dict):
        """发送 JSON-RPC 通知（无 id，无响应）。"""
        req = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception:
            pass

    def _read_loop(self):
        """后台线程：持续读取 MCP Server 的 stdout 响应。"""
        while self._proc and self._proc.poll() is None:
            try:
                line = self._proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                resp = json.loads(line)
                rid = resp.get("id")
                if rid is not None:
                    self._results[rid] = resp
            except (json.JSONDecodeError, Exception):
                continue

    def _read_stderr(self):
        """后台线程：持续读取 MCP Server 的 stderr（防止 pipe 满导致进程卡死）。"""
        while self._proc and self._proc.poll() is None:
            try:
                line = self._proc.stderr.readline()
                if not line:
                    break
                line = line.rstrip()
                if line:
                    # 转发到 GUI 日志（dim级别，不刷屏）
                    self._log(f"[MCP] {line}", "dim")
            except Exception:
                continue

# ── 工具列表格式化（供 DS system prompt 使用）──

def format_tools_for_ds(tools: list[dict]) -> str:
    """将 MCP 工具列表格式化为 DS 能理解的 Markdown 表格。"""
    lines = ["| 工具 | 说明 | 参数 |"]
    lines.append("|------|------|------|")
    for t in tools:
        name = t.get("name", "")
        desc = t.get("description", "").split("\n")[0][:60]  # 只取第一行
        schema = t.get("inputSchema", {})
        props = schema.get("properties", {})
        required = schema.get("required", [])
        params = []
        for pname, pinfo in props.items():
            mark = "*" if pname in required else ""
            ptype = pinfo.get("type", "?")
            params.append(f"{mark}{pname}({ptype}){mark}")
        param_str = ", ".join(params) if params else "—"
        lines.append(f"| `{name}` | {desc} | {param_str} |")
    return "\n".join(lines)
