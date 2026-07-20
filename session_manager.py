"""
会话管理器 — JSON 驱动的会话存储与 CRUD。

设计:
- sessions/sessions.json 作为会话索引（单一真理源）
- 每个会话是 sessions/ 下的独立文件夹
- DS 通过 MCP 工具自由读写，不再依赖 hardcode 关键词

文件夹结构:
  sessions/
    sessions.json                    ← 索引（DS 可读）
    20260719_1135_ROS2入门21讲/
      meta.json                      ← 会话元数据
      screenshots/                   ← 截图
      captions_*.txt                 ← 字幕
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(ROOT, "sessions")
INDEX_PATH = os.path.join(SESSIONS_DIR, "sessions.json")


def _ensure_dirs():
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def _load_index() -> list[dict]:
    """加载会话索引。"""
    _ensure_dirs()
    if not os.path.exists(INDEX_PATH):
        return []
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def _save_index(sessions: list[dict]):
    """保存会话索引。"""
    _ensure_dirs()
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)


def list_sessions() -> dict:
    """列出所有会话，含元数据摘要。

    返回:
        {"sessions": [...], "count": N, "current": "folder_name" | None}
    """
    sessions = _load_index()
    # 只保留摘要字段，不暴露完整路径
    summary = []
    for s in sessions:
        summary.append({
            "name": s.get("name", ""),
            "title": s.get("title", ""),
            "created_at": s.get("created_at", ""),
            "screenshot_count": s.get("screenshot_count", 0),
            "last_used": s.get("last_used", ""),
        })
    return {
        "sessions": summary,
        "count": len(summary),
    }


def create_session(name: str, title: str = "", inherit_from: str = "") -> dict:
    """创建新会话文件夹。

    Args:
        name:    会话名称（用作文件夹名的一部分）
        title:   视频/课程标题（人类可读）
        inherit_from: 要继承的会话名称（复制截图 + 字幕到新会话）

    返回:
        {"name": "...", "path": "...", "inherited_from": "..." | None}
    """
    _ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _safe_name(name)
    folder_name = f"{ts}_{safe_name}" if safe_name else ts
    folder_path = os.path.join(SESSIONS_DIR, folder_name)

    # 创建文件夹结构
    os.makedirs(os.path.join(folder_path, "screenshots"), exist_ok=True)

    # 写入 meta.json
    meta = {
        "name": folder_name,
        "title": title or name,
        "created_at": datetime.now().isoformat(),
        "screenshot_count": 0,
        "caption_file": "",
    }
    with open(os.path.join(folder_path, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 继承旧会话数据
    inherited = None
    if inherit_from:
        inherited = _inherit_session(inherit_from, folder_path)
        meta["inherited_from"] = inherit_from
        meta["screenshot_count"] = inherited.get("screenshots_copied", 0)
        if inherited.get("caption_copied"):
            meta["caption_file"] = inherited.get("caption_file", "")
        with open(os.path.join(folder_path, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    # 更新索引
    sessions = _load_index()
    sessions.insert(0, {
        "name": folder_name,
        "title": title or name,
        "path": folder_path,
        "created_at": meta["created_at"],
        "screenshot_count": meta["screenshot_count"],
        "last_used": meta["created_at"],
    })
    _save_index(sessions)

    return {
        "name": folder_name,
        "path": folder_path,
        "title": meta["title"],
        "inherited_from": inherit_from,
        "screenshot_count": meta["screenshot_count"],
    }


def delete_session(name: str) -> dict:
    """删除会话及其文件夹。"""
    sessions = _load_index()
    target = None
    for s in sessions:
        if s["name"] == name:
            target = s
            break

    if not target:
        return {"status": "error", "message": f"会话 '{name}' 不存在"}

    # 删除文件夹
    folder_path = target.get("path", "")
    if folder_path and os.path.isdir(folder_path):
        shutil.rmtree(folder_path)

    # 从索引移除
    sessions = [s for s in sessions if s["name"] != name]
    _save_index(sessions)

    return {"status": "deleted", "name": name}


def get_session_info(name: str = "") -> dict:
    """获取会话详情。

    Args:
        name: 会话名称，为空则返回最近一次使用的会话（索引第一条）

    返回:
        {"name": "...", "meta": {...}, "screenshots": [...], "captions": "..."}
    """
    sessions = _load_index()
    if not sessions:
        return {"status": "empty", "message": "没有任何会话"}

    target = None
    if name:
        for s in sessions:
            if s["name"] == name:
                target = s
                break
    else:
        target = sessions[0]  # 最新的

    if not target:
        return {"status": "error", "message": f"会话 '{name}' 不存在"}

    folder_path = target.get("path", "")
    result = {
        "name": target["name"],
        "title": target.get("title", ""),
        "created_at": target.get("created_at", ""),
        "path": folder_path,
    }

    # 读 meta.json
    meta_path = os.path.join(folder_path, "meta.json")
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            result["meta"] = json.load(f)

    # 统计截图
    ss_dir = os.path.join(folder_path, "screenshots")
    if os.path.isdir(ss_dir):
        files = sorted([f for f in os.listdir(ss_dir) if f.endswith(".png")])
        result["screenshots"] = files
        result["screenshot_count"] = len(files)
    else:
        result["screenshots"] = []
        result["screenshot_count"] = 0

    # 找字幕文件
    caption_files = []
    if os.path.isdir(folder_path):
        for f in os.listdir(folder_path):
            if f.startswith("captions_") and f.endswith(".txt"):
                caption_files.append(os.path.join(folder_path, f))
    result["caption_file"] = caption_files[0] if caption_files else ""

    return result


def get_screenshot_dir(name: str) -> str:
    """获取会话的截图目录路径（不存在则创建）。"""
    info = get_session_info(name)
    folder = info.get("path", "")
    if not folder:
        return ""
    ss_dir = os.path.join(folder, "screenshots")
    os.makedirs(ss_dir, exist_ok=True)
    return ss_dir


def update_session_meta(name: str, updates: dict) -> dict:
    """更新会话元数据（如截图计数）。"""
    sessions = _load_index()
    for s in sessions:
        if s["name"] == name:
            s.update(updates)
            s["last_used"] = datetime.now().isoformat()
            folder = s.get("path", "")
            if folder and os.path.isdir(folder):
                meta_path = os.path.join(folder, "meta.json")
                if os.path.exists(meta_path):
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    meta.update(updates)
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)
            _save_index(sessions)
            return {"status": "updated", "name": name}
    return {"status": "error", "message": f"会话 '{name}' 不存在"}


# ── 内部辅助 ──

def _safe_name(name: str) -> str:
    """去除非法字符，限制长度。"""
    import re
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name[:50]


def _inherit_session(source_name: str, dest_folder: str) -> dict:
    """从旧会话复制截图和字幕到新会话。"""
    result = {"screenshots_copied": 0, "caption_copied": False, "caption_file": ""}

    info = get_session_info(source_name)
    if info.get("status") == "error":
        return result

    src_folder = info.get("path", "")
    if not src_folder or not os.path.isdir(src_folder):
        return result

    # 复制截图
    src_ss = os.path.join(src_folder, "screenshots")
    dst_ss = os.path.join(dest_folder, "screenshots")
    os.makedirs(dst_ss, exist_ok=True)
    if os.path.isdir(src_ss):
        for f in os.listdir(src_ss):
            if f.endswith(".png"):
                shutil.copy2(os.path.join(src_ss, f), os.path.join(dst_ss, f))
                result["screenshots_copied"] += 1

    # 复制字幕
    src_cap = info.get("caption_file", "")
    if src_cap and os.path.exists(src_cap):
        dst_cap = os.path.join(dest_folder, os.path.basename(src_cap))
        shutil.copy2(src_cap, dst_cap)
        result["caption_copied"] = True
        result["caption_file"] = dst_cap

    return result
