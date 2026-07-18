# VideoAgent v2.1

> 桌面悬浮 AI 视频分析助手 — 用自然语言操控浏览器，自动看视频、截图、做笔记。

在 Edge 中打开任何视频，VideoAgent 悬浮在屏幕角落。你可以用自然语言跟它对话——"帮我找 CNN 教程"、"跳到 5 分钟"、"总结一下这段讲了什么"——它会自动操控浏览器完成。

## 历史性突破

v2.1 实现了三个关键跃迁：

### 1. 自然语言操控浏览器

聊天框不再只是关键词匹配，而是接入 DeepSeek 实现真正的对话式控制：

- **搜索视频**：说「帮我找 PyTorch 教程」，DS 自动帮你搜索并导航
- **远程操控**：「跳转到 10 分钟」「暂停截图」「继续播放」——全部由 AI 理解并执行
- **理解上下文**：DS 知道当前视频的标题、进度、播放状态，回复更精准

### 2. DS 直接控制视频播放器

不再只是"DS 出截图名单、代码去执行"。现在是完整的动作链路：

```
DS 收到字幕 + 视频状态
  → 决定：这页公式值得截图
  → 输出：seek(245s) → pause() → screenshot("CNN公式推导") → play()
  → 代码按序执行全部动作
```

### 3. 智能视频识别

不只认 `<video>` 标签。自动扫描页面上的：
- 自定义播放器（B站、YouTube、Video.js、腾讯等 12 种）
- `<iframe>` 嵌入式播放器
- `<canvas>` WebGL 渲染
- LLM 辅助决策，选中后精准定位播放器内 video 元素

## 效果

```
你: 帮我分析这个视频
  → AI 自动:
    1. 连接浏览器 (CDP，不碰 Cookie/登录态)
    2. 启动 Whisper 实时听音频
    3. 每分钟: 读新增字幕 → DeepSeek 判断截图点 → 精确截图视频画面

你: 帮我找 PyTorch 入门教程
  → AI 自动在 B 站搜索并打开结果页

你: 跳到 5 分钟截个图
  → AI seek 300s → pause → screenshot("PyTorch基础") → play

输出:
  sessions/20260718_PyTorch入门教程/
    ├── captions_20260718_1530.txt    完整字幕（按会话独立）
    └── screenshots/
        ├── 0245s_CNN架构图.png        DS 命名，只截视频画面
        └── 0500s_公式推导.png
```

## 架构

```
agent_v2.py (入口, tkinter GUI)
├── agent_gui.py             透明悬浮窗（聊天 + 日志 + 配置面板）
├── video_agent.py           核心编排器 — 用户消息路由 & 动作执行
│   ├── browser_controller.py   CDP 浏览器操控（Playwright）
│   │   ├── 页面扫描 (12 种播放器识别)
│   │   ├── 视频元素截图（不含弹幕/评论区）
│   │   ├── seek/pause/play/navigate
│   │   └── 独立 Profile 管理（不干扰用户浏览器）
│   ├── deepseek_client.py      DS v4 Flash API
│   │   ├── chat() 通用对话 + 动作解析
│   │   ├── analyze_screenshots() 分析循环用
│   │   └── 多轮对话历史管理
│   └── transcriber_core.py     Whisper 实时音频转录
├── live_caption_video.py     独立悬浮字幕窗口 (可选)
└── transcriber_mcp.py        MCP Server 版转录 (可选)
```

## 快速开始

### 1. 安装依赖

```bash
pip install python-dotenv playwright requests soundcard faster-whisper numpy
playwright install chromium
```

### 2. 配置 API Key

```bash
copy .env.example .env
# 编辑 .env，填入 DeepSeek API Key（获取地址见 .env.example）
```

### 3. 运行

```bash
python agent_v2.py
```

程序会自动检测 Edge 并启动独立的调试模式浏览器，**不影响你平时的 Edge**。首次需要手动登录一次视频网站（Cookie 会持久化保存）。

### 4. 开始对话

在聊天框中输入：

| 指令 | 效果 |
|---|---|
| 帮我分析这个视频 | 一键启动完整分析流程 |
| 帮我找 CNN 教程 | AI 帮你搜索并打开（B站） |
| 跳转到 5 分钟 | 快进到 300s |
| 暂停然后截图 | pause + screenshot |
| 总结一下 | AI 根据字幕总结 |
| 停止 | 停止分析 |
| 新会话 | 强制新建文件夹 |
| 设置密钥 sk-xxx | 动态配置 API Key（持久化） |

任何自然语言指令都支持——AI 会理解并执行，不懂的会文字回复你。

## 核心设计

### 会话管理（v2.1）

每次分析自动创建独立会话文件夹，以视频标题命名：

```
sessions/
├── 20260717_CNN入门教程/       ← 含时间戳 + 视频标题
│   ├── captions_20260717.txt
│   └── screenshots/
└── 20260717_PyTorch实战/
    ├── captions_20260717.txt   ← 每个会话的字幕完全独立
    └── screenshots/
```

**续接分析**：同一视频中途暂停再继续，自动复用原文件夹，不重复创建。

### 增量字幕读取

用行号追踪已读位置，每轮只取 DeepSeek 未见过的新增字幕，避免重复处理、节省 token。

### 轻量化截图策略

- 每轮分析间隔 60 秒
- 每轮最多 3 张截图
- 纯闲聊过渡话不截图
- 优先截图表、公式、代码、架构

### 视频元素截图

截图只截 `<video>` 标签本身，不包含弹幕、评论区、侧边栏。三层容错：元素可见 → 整页回退 → 兜底。

### 自动停止

视频播至剩余 ≤3s 时，AI 自动处理最后一轮字幕后停止分析，生成完成摘要。

## 文件说明

| 文件 | 作用 |
|---|---|
| `agent_v2.py` | GUI 入口 |
| `agent_gui.py` | 透明悬浮窗（聊天+日志+配置面板） |
| `video_agent.py` | 核心编排 + 用户指令路由 |
| `browser_controller.py` | CDP 浏览器操控（Playwright） |
| `deepseek_client.py` | DeepSeek API 客户端 |
| `transcriber_core.py` | 音频采集 + Whisper 转录 |
| `live_caption_video.py` | 独立悬浮字幕 GUI |
| `transcriber_mcp.py` | MCP 版转录服务 |
| `.env.example` | 配置模板（不含密钥） |

`.env` 文件已通过 `.gitignore` 排除，避免密钥泄露。

## License

MIT
