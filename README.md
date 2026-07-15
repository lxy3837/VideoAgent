# VideoAgent — 让 AI Agent 学会看视频

> 灵感来源：傻逼中期实训给一堆没字幕的视频看，听都听不清。既然能 [实时扬声器转字幕](https://github.com/lxy3837/live-caption)，那能不能让 Agent 自己学会看视频？

## 这是什么

一个多模态 AI Agent，能自主操控浏览器播放视频、截图、读字幕、理解内容。

```
LLM (大脑) → 预测关键时间点
    → Playwright 操控 Edge 跳转、播放、暂停
    → 截图 + OCR (眼睛)
    → Whisper 实时转录 (耳朵)
    → 全部按 video.currentTime 对齐 (统一锚点)
    → LLM 理解视频内容
```

## 架构

```
agent.py          ← Agent 决策层（你要自己设计的地方）
src/index.ts      ← MCP Server 执行层（10 个视频操控工具）
```

| 层 | 技术 | 职责 |
|---|---|---|
| 大脑 | LLM (vision) | 看截图预测关键帧、理解内容 |
| 眼睛 | 简单 OCR 模型 | 读画面上的文字 |
| 耳朵 | Whisper (live_caption) | 实时扬声器转字幕 |
| 手 | Playwright + Edge | 播放/暂停/seek/截图 |
| 通信 | MCP 协议 (stdio) | Agent ↔ Server |

## 当前状态

正在开发中，目前不保证能运行。

Roadmap:
- [x] MCP Server 基础视频操控工具
- [x] seek → 等帧就绪 → 截图闭环
- [x] 时间戳锚点统一对齐
- [x] Agent 粗扫描 + 精确捕获策略
- [x] 暂停消除 whisper 延迟方案
- [ ] OCR 模型接入
- [ ] LLM API 接入
- [ ] live_caption 字幕桥接
- [ ] 端到端可运行

## License

MIT
