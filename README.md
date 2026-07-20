# VideoAgent v2.3

> 桌面 AI 浏览器助手 — 用自然语言操控浏览器，能看、能点、能分析。

打开浏览器任一页面，VideoAgent 悬浮在屏幕角落。"帮我打开第10讲"、"这个页面有什么"、"开始分析这个视频"——AI 自己看页面、自己点按钮、自己执行下一步。

## v2.3 新特性

### 1. LLM 自主认识页面 — 11 维全量扫描

不再替 AI 做判断，把选择权交给 LLM：

- **原生标签分类**：返回 `a`/`button`/`iframe`/`video`/`h1`，语义明确
- **Shadow DOM 穿透**：递归扫描 Web Component 自定义元素的 shadowRoot
- **`<object>`/`<embed>` 检测**：兼容老旧播放器嵌入方式
- **ARIA 角色识别**：`role="tab"`/`treeitem`/`menuitem`/`gridcell` 等无障碍标签
- **文本块捕获**：段落级文字内容（非交互，但给 LLM 理解页面上下文）
- **列表结构**：`<li>`/`<dt>`/`<dd>` 课程目录、章节列表
- **统计摘要**：`{total_iframes:1, total_links:35, total_buttons:3, ...}` LLM 一眼看懂页面类型

之前 DS 遇到非标视频站反复要 `get_page` 的死循环消失 — DS 收到完整页面快照后自己判断"这是个视频页，不需要再扫了"。

### 2. MCP 进程分离架构

Agent 和浏览器通过 stdio JSON-RPC 通信，进程级隔离：

- **MCP Server** (`mcp_server.py`)：独立子进程，持有 Playwright 实例，暴露 20 个工具
- **MCP Client** (`mcp_client.py`)：GUI 侧客户端，握手失败自动重试（15s→30s→45s 渐进超时）
- **服务端崩溃不影响 GUI**：子进程挂了 GUI 自动重启

### 3. 编码 + COM 系统级修复

- **stdout/stderr 编码**：`sys.stdout.reconfigure(encoding="utf-8")` 全局锁定，特殊字符不再报 `UnicodeEncodeError`
- **subprocess 管道编码**：`encoding="utf-8"` 对齐两端，中文不乱码
- **Transcriber 延迟导入**：`soundcard` + `faster-whisper`（含 COM 初始化）只在真正需要转录时才加载，MCP 握手秒过
- **`_COMLibrary.__del__` monkey-patch**：静默处理 Python 退出时 COM 先清理导致的 `AttributeError`

## 既存能力（v2.2）

### 1. AI 长眼睛了——通用页面感知

不再只认 `<video>` 标签。AI 能"看到"任何网页上有什么：

- **AX Tree（无障碍树）**：借鉴 browser-harness 的 CDP 原生方案，浏览器自动生成的语义化页面结构，不依赖 CSS class 名称
- **DOM 扫描兜底**：AX tree 为空时（SPA 用 div+click 替代 a/button）自动回退 JS DOM 遍历
- **SPA 渲染等待**：`navigate()` 等待 Vue/#app/#root 挂载点就绪后再提取，小鹅通、知识付费等 Vue 页面不再空白
- **非视频页识别**：课程列表、搜索结果、B站首页——AI 能看懂链接、按钮、标题，自主决策

```
👤 你: 这个页面有什么？
🤖 AI: 当前页面: ROS2入门21讲 · 古月学院
     链接: 第1讲·ROS2介绍 → ... | 第2讲·环境搭建 → ... | ...
     按钮: 立即学习, 加入课程
```

### 2. AI 有手了——点击页面元素

AI 不只是看，还能**主动点击**：

- `click(text="第10讲·通信接口")` — 按可见文本点击
- `click(text="立即学习", index=2)` — 第3个同名按钮
- `click(selector=".course-item:nth-child(10)")` — CSS 选择器精确点击
- **三层容错**：Playwright text 匹配 → 模糊匹配 → JS 兜底（绕过遮罩/disabled）

```
👤 你: 帮我打开第10讲
🤖 AI: （已看到页面元素"第10讲·通信接口"）
  → click(text="第10讲·通信接口")
  → 自动看新页面 → "已打开第十讲，要我播放吗？"
```

### 3. AI 不用等——动作链式自动执行

告别"做完一件事就停下"。navigate/search/click 后自动获取新页面上下文，连续调用 DS 决策下一步：

```
用户: "帮我打开古月居ROS课程"
  → DS: navigate(url)
  → 系统执行导航 → 自动取新页面内容
  → 续问 DS: "当前页面: ROS2入门21讲 | 链接: 第1讲... | 按钮: 立即学习"
  → DS: "这是ROS课程主页，你要看哪一讲？"
  → 输出给用户 ✓
```

不再出现"已打开链接"然后就没下文的尴尬。

### 4. 精准标签页锁定

多窗口、NTP 空白页、后台标签页——AI 只操作你正在看的那个：

- `document.visibilityState` 过滤隐藏标签页
- `document.hasFocus()` 区分多窗口场景
- NTP 过滤（`edge://` `chrome://` `about:` `ntp.msn.cn`）排除空白页
- `_start_agent()` 不再扫描 video 标签，任何页面都能直接连接

## 效果

```
启动: "启动Agent"
  → AI 连接当前浏览器 → 锁定你正在看的标签页

你: 当前页面是古月居的ROS课程吗？
  → AI 直接看到并回复（不再说"页面为空"）

你: 打开第10讲
  → AI 点击"第10讲·通信接口" → 自动进入视频页 → 问你"要播放吗？"

你: 帮我分析这个视频
  → AI 自动:
    1. 启 Whisper 实时转录
    2. 每分钟: 读新增字幕 → DS 判断截图点 → 精确截图视频画面

输出:
  sessions/20260720_ROS2入门21讲/
    ├── captions_20260720_1530.txt    完整字幕
    └── screenshots/
        ├── 0030s_通信架构.png        DS 命名，只截视频
        └── 0150s_Topic示意图.png
```

## 架构

```
agent_v2.py (入口, tkinter GUI)
├── agent_gui.py             透明悬浮窗（聊天 + 日志 + 配置面板）
├── video_agent.py           核心编排器 — 用户消息路由 & 动作链
│   ├── mcp_client.py            MCP 客户端 — stdio JSON-RPC 通信，渐进超时重试
│   ├── mcp_server.py            MCP Server — 独立子进程，持有 Playwright 实例，20 个工具
│   ├── browser_controller.py    CDP 浏览器操控（Playwright）
│   │   ├── scan_page()              全量页面扫描（Shadow DOM + 11 维）
│   │   ├── get_page_ax_tree()       AX Tree 提取（browser-harness 方案）
│   │   ├── click_element()          点击页面元素
│   │   ├── ensure_active_tab()      精准标签页锁定
│   │   ├── navigate/seek/pause/play/screenshot
│   │   └── 独立 Profile 管理（不干扰用户浏览器）
│   ├── deepseek_client.py       DeepSeek API 客户端
│   │   ├── chat() 通用对话 + 动作解析
│   │   ├── analyze_screenshots() 分析循环
│   │   └── 多轮对话历史管理
│   ├── session_manager.py       会话文件管理（截图/字幕/JSON 持久化）
│   └── transcriber_core.py      Whisper 实时音频转录（延迟导入）
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

程序会自动检测 Edge 并启动独立的调试模式浏览器，**不影响你平时的 Edge**。首次需要手动登录一次视频网站（Cookie 持久化保存）。

### 4. 开始对话

在聊天框中输入：

| 指令 | 效果 |
|---|---|
| 启动Agent | 连接浏览器，锁定当前标签页 |
| 当前页面是什么？ | AI 自动识别页面内容并描述 |
| 帮我分析这个视频 | 一键启动字幕+截图分析 |
| 打开第10讲 | AI 看页面 → 找到并点击"第10讲" |
| 跳转到 5 分钟 | 快进到 300s |
| 暂停然后截图 | pause + screenshot |
| 帮我找 CNN 教程 | AI 搜索并打开（B站） |
| 停止 | 停止分析 |
| 新会话 | 强制新建文件夹 |

任何自然语言指令都支持——AI 自己看页面、自己做决策。

## 核心设计

### 全量页面扫描（v2.3）

每次对话自动附带完整页面快照：标题、URL、**统计摘要**、iframe/object 列表、所有链接/按钮、文本段落、列表项。LLM 根据完整数据自主判断页面类型，Agent 不做预设分类。

扫描维度：Shadow DOM → 标题 → iframe/object/embed → video/audio → 链接 → 按钮 → ARIA 角色 → 输入框 → 文本块 → 列表项 → 图片 → 可点击元素。最多 200 个元素，去重、只保留可见元素。

### MCP 进程隔离（v2.3）

Agent GUI 和浏览器操控跑在不同进程里，通过 stdio JSON-RPC 通信。服务端崩溃不影响主 GUI，客户端自动重启。握手超时渐进重试（15s→30s→45s）。

### 页面感知（v2.2）

每次对话自动附带当前页面结构：标题、URL、链接列表、按钮、搜索框。非视频页用 AX Tree + DOM 双通道提取，SPA 页面自动等待渲染完成。

### 动作链（v2.2）

navigate / search / click 执行后自动获取新页面上下文，续问 DS 做下一步决策。最多 1 次续问，防止无限循环。

### 页面交互（v2.2）

AI 能点击页面上的按钮、链接、列表项。按文本匹配（优先）、CSS 选择器。Playwright + JS 双容错。

### 会话管理

每次分析自动创建独立会话文件夹，以视频标题+时间戳命名。同一视频中断再继续自动复用原文件夹。

### 增量字幕读取

行号追踪已读位置，每轮只取新增字幕，避免重复处理、节省 token。

### 轻量化截图策略

- 每轮分析间隔 60 秒
- 每轮最多 3 张截图
- 纯闲聊过渡话不截图
- 优先截图表、公式、代码、架构

### 自动停止

视频剩余 ≤3s 时，AI 自动处理最后一轮字幕后停止分析并生成摘要。

## 文件说明

| 文件 | 作用 |
|---|---|
| `agent_v2.py` | GUI 入口 |
| `agent_gui.py` | 透明悬浮窗（聊天+日志+配置面板） |
| `video_agent.py` | 核心编排 + 用户指令路由 + 动作链 |
| `mcp_client.py` | MCP 客户端 — stdio 通信 + 握手重试 |
| `mcp_server.py` | MCP Server — 独立进程，20 个浏览器操控工具 |
| `browser_controller.py` | CDP 浏览器操控（Playwright）— scan_page/clip视频/seeker |
| `deepseek_client.py` | DeepSeek API 客户端 |
| `session_manager.py` | 会话文件夹管理（截图/字幕持久化） |
| `transcriber_core.py` | 音频采集 + Whisper 转录（延迟导入） |
| `live_caption_video.py` | 独立悬浮字幕 GUI |
| `transcriber_mcp.py` | MCP 版转录服务 |
| `.env.example` | 配置模板（不含密钥） |

`.env` 文件已通过 `.gitignore` 排除，避免密钥泄露。

## 版本历史

Git 自动保留所有历史版本。查看旧版 README：

```bash
git log --oneline -- README.md          # 所有 README 变更记录
git show 96bcf97:README.md             # 还原某次 commit 时的 README 内容
```

## License

MIT
