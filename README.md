# VideoAgent

> 让 AI Agent 学会看视频。

灵感来自一次没有字幕的实训课——既然能 [实时扬声器转字幕](https://github.com/lxy3837/live-caption)，那能不能更进一步，让 Agent 自己去看、去听、去理解视频内容？

## 这是什么

VideoAgent 是一个多模态 AI Agent，能够自主操控浏览器播放视频、截取画面、读取字幕、理解视频内容。

```
LLM (大脑) → 预测关键时间点
    → Playwright 操控 Edge，跳转 / 播放 / 暂停
    → 截图 + OCR 识别画面文字 (眼睛)
    → Whisper 实时扬声器转录 (耳朵)
    → 全部按 video.currentTime 对齐
    → LLM 综合理解视频内容
```

## 架构

| 组件 | 技术栈 | 职责 |
|---|---|---|
| Agent 决策层 | Python (`agent.py`) | 粗扫描、精确捕获、LLM 调度、时间戳对齐 |
| MCP 执行层 | TypeScript (`src/index.ts`) | 10 个视频操控工具，Playwright 驱动 Edge |
| 通信协议 | MCP (Model Context Protocol) / stdio | Agent ↔ Server 指令下发与结果回传 |
| 字幕来源 | Whisper (`live_caption.py`) | 系统扬声器实时语音转文字 |
| 浏览内核 | Microsoft Edge (Chromium) | 视频播放载体 |

## 工具列表

| 工具 | 功能 |
|---|---|
| `video_navigate` | 打开视频页面 |
| `video_play` / `video_pause` | 播放 / 暂停 |
| `video_seek` | 精准跳转（等待 seeked 事件 + 轮询帧就绪） |
| `video_capture_at` | 跳转 → 等帧就绪 → 截图 → 返回图片 + 时间戳 |
| `video_capture_sequence` | 区间等间隔批量采样 |
| `video_screenshot` | 截图（带 video.currentTime 标记） |
| `video_get_state` | 完整视频状态快照 |
| `video_exec_js` | 执行自定义 JS |
| `video_close` | 关闭浏览器 |

## 核心设计

### 时间戳锚点

所有采集数据（截图、OCR、字幕）统一用 `video.currentTime` 作为时间戳锚点。Agent 按锚点对齐多模态数据，无需考虑各模块的处理延迟。

### 暂停消除延迟

Whisper 实时转录有 1-2 秒延迟。Agent 采用 "play N 秒 → pause → 等 whisper 追上" 策略，视频暂停后 whisper 输出的内容精确对应刚刚播放的片段，延迟被暂停消除。

### 粗扫描 + 精确捕获

1. 粗扫描：对视频 5%-95% 区间等间隔采样，LLM 用视觉能力快速定位目标
2. 精确捕获：seek 到目标位置，播放一段让 whisper 采集音频，暂停后截帧 + 读字幕

## 当前状态

正在开发中，目前不保证能运行。

- [x] MCP Server 视频操控工具
- [x] seek → 帧就绪 → 截图闭环
- [x] 时间戳锚点统一对齐
- [x] 粗扫描 + 精确捕获策略
- [x] 暂停消除 whisper 延迟
- [ ] OCR 模型接入
- [ ] LLM API 接入
- [ ] live_caption 字幕桥接
- [ ] 端到端可运行

## 快速开始

```bash
# 安装依赖
npm install

# 编译 MCP Server
npx tsc

# 安装 Playwright 浏览器（已安装 Edge 可跳过）
npx playwright install chromium

# 启动 MCP Server
node dist/index.js

# 启动 Agent
python agent.py
```

## License

MIT
