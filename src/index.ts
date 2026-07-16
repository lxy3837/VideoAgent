/**
 * MCP Video Agent Server — 可靠视频截图版
 * 
 * 核心哲学（参考 live_caption 的滑动窗口 + local agreement 思路）：
 *   不发完指令就截图，而是 "seek → 等事件确认 → 轮询验证 → 缓冲就绪 → 截图 + 元数据 → Agent 判决"
 * 
 * 时序保证链：
 *   1. video.currentTime = target
 *   2. 等待 'seeked' 事件触发
 *   3. 轮询 currentTime，直到 |currentTime - target| < tolerance
 *   4. 等待 'canplay' (当前帧已解码就绪)
 *   5. 截图 + 返回 {actualTime, drift, bufferedRange}
 *   6. Agent 根据 drift 决定是否重试
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { chromium, type Browser, type BrowserContext, type Page } from "playwright";
import { existsSync, mkdirSync, readFileSync } from "fs";
import path from "path";

// ---- 全局状态 ----
let browser: Browser | null = null;
let context: BrowserContext | null = null;
let page: Page | null = null;

const SCREENSHOT_DIR = path.resolve(process.cwd(), "screenshots");

// ---- 辅助 ----

function ensureScreenshotDir(): void {
  if (!existsSync(SCREENSHOT_DIR)) mkdirSync(SCREENSHOT_DIR, { recursive: true });
}

async function getPage(): Promise<Page> {
  if (page && !page.isClosed()) return page;
  if (browser) { try { await browser.close(); } catch { /* */ } }

  browser = await chromium.launch({
    channel: "msedge",
    headless: false,
    args: [
      "--autoplay-policy=no-user-gesture-required",
      "--disable-blink-features=AutomationControlled",
    ],
  });
  context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
  });
  page = await context.newPage();
  return page;
}

// ═══════════════════════════════════════════════════════════
// 核心：视频帧就绪检测（注入到浏览器端的 JS 工具函数）
// ═══════════════════════════════════════════════════════════

/** 
 * 在页面中等待视频 seek 完成并帧就绪。
 * 返回 { ok, currentTime, readyState, bufferedEnd, error }
 */
function injectSeekWaitScript(targetSeconds: number, tolerance: number, maxWaitMs: number) {
  return `
    (async () => {
      const video = document.querySelector("video");
      if (!video) return { ok: false, error: "no video element found" };

      const target = ${targetSeconds};
      const tol = ${tolerance};
      const deadline = Date.now() + ${maxWaitMs};

      // 如果视频时长未知或目标超出范围，直接返回错误
      if (isNaN(video.duration)) return { ok: false, error: "video duration unknown" };
      const clamped = Math.min(target, video.duration - 0.1);
      if (clamped < 0) return { ok: false, error: "target < 0" };

      // 如果已经在目标位置（误差 < tolerance），直接返回
      if (Math.abs(video.currentTime - clamped) < tol && video.readyState >= 2) {
        return {
          ok: true,
          currentTime: video.currentTime,
          targetTime: clamped,
          drift: video.currentTime - clamped,
          readyState: video.readyState,
          bufferedEnd: video.buffered.length > 0 ? video.buffered.end(video.buffered.length - 1) : -1,
          method: "already-at-target",
        };
      }

      // 设置 currentTime
      video.currentTime = clamped;

      // 等待 seeked 事件
      let seeked = false;
      const onSeeked = () => { seeked = true; };
      video.addEventListener("seeked", onSeeked, { once: true });

      // 轮询等待条件满足
      while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 50));

        if (seeked && Math.abs(video.currentTime - clamped) < tol) {
          // 再等 canplay 确保帧解码完毕
          if (video.readyState >= 3) { // HAVE_FUTURE_DATA = 3
            video.removeEventListener("seeked", onSeeked);
            return {
              ok: true,
              currentTime: video.currentTime,
              targetTime: clamped,
              drift: video.currentTime - clamped,
              readyState: video.readyState,
              bufferedEnd: video.buffered.length > 0
                ? video.buffered.end(video.buffered.length - 1) : -1,
              waitMs: Date.now() - (deadline - ${maxWaitMs}),
              method: "seeked+poll",
            };
          }
          // readyState < 3: 触发一次 canplay 等待
          await new Promise(r => {
            const onCanPlay = () => { r(undefined); };
            video.addEventListener("canplay", onCanPlay, { once: true });
            setTimeout(() => { video.removeEventListener("canplay", onCanPlay); r(undefined); }, 3000);
          });
        }
      }

      video.removeEventListener("seeked", onSeeked);

      // 超时：返回当前实际状态
      return {
        ok: false,
        error: "timeout waiting for frame",
        currentTime: video.currentTime,
        targetTime: clamped,
        drift: video.currentTime - clamped,
        readyState: video.readyState,
        bufferedEnd: video.buffered.length > 0
          ? video.buffered.end(video.buffered.length - 1) : -1,
        seeked,
        waitedMs: ${maxWaitMs},
      };
    })()
  `;
}

/** 获取视频详细状态快照 */
function injectStateSnapshot() {
  return `
    (() => {
      const videos = document.querySelectorAll("video");
      if (videos.length === 0) return { hasVideo: false };

      const result = [];
      for (const v of videos) {
        const buffered = [];
        for (let i = 0; i < v.buffered.length; i++) {
          buffered.push({ start: v.buffered.start(i), end: v.buffered.end(i) });
        }
        result.push({
          paused: v.paused,
          ended: v.ended,
          currentTime: v.currentTime,
          duration: v.duration,
          playbackRate: v.playbackRate,
          volume: v.volume,
          muted: v.muted,
          readyState: v.readyState,
          readyStateName: ["HAVE_NOTHING","HAVE_METADATA","HAVE_CURRENT_DATA","HAVE_FUTURE_DATA","HAVE_ENOUGH_DATA"][v.readyState] || "UNKNOWN",
          networkState: v.networkState,
          networkStateName: ["NETWORK_EMPTY","NETWORK_IDLE","NETWORK_LOADING","NETWORK_NO_SOURCE"][v.networkState] || "UNKNOWN",
          buffered,
          src: v.currentSrc || v.src,
          width: v.videoWidth,
          height: v.videoHeight,
        });
      }
      return { hasVideo: true, videos: result, url: window.location.href, title: document.title };
    })()
  `;
}

// ═══════════════════════════════════════════════════════════
// MCP Server
// ═══════════════════════════════════════════════════════════

const server = new McpServer({
  name: "mcp-video-agent",
  version: "2.0.0",
});

// ── 工具 1: video_navigate ──────────────────────────────
server.tool(
  "video_navigate",
  "打开视频网页。自动等待页面及播放器加载完成。",
  {
    url: z.string().describe("视频网页 URL"),
    cookies: z.string().optional().describe("可选的 Cookie 字符串（格式: 'key1=val1; key2=val2'），用于登录态"),
    waitUntil: z.enum(["load", "domcontentloaded", "networkidle"]).default("networkidle")
      .describe("等待策略：networkidle(默认/最稳)/load/domcontentloaded(快)"),
  },
  async ({ url, cookies, waitUntil }) => {
    try {
      // 如果传了 cookie，注入到浏览器上下文
      if (cookies && context) {
        const domain = new URL(url).hostname;
        const cookieList = cookies.split(";").map(c => c.trim()).filter(c => c && c.includes("=")).map(c => {
          const [name, ...rest] = c.split("=");
          return {
            name: name.trim(),
            value: rest.join("=").trim(),
            domain: domain.startsWith("www.") ? domain : `.${domain}`,
            path: "/",
          };
        });
        if (cookieList.length > 0) {
          await context.addCookies(cookieList);
        }
      }
      const p = await getPage();
      await p.goto(url, { waitUntil, timeout: 30000 });

      // 等待播放器初始化：轮询直到 video 元素 readyState >= 1
      await p.waitForFunction(() => {
        const v = document.querySelector("video");
        return v && v.readyState >= 1;
      }, { timeout: 8000 }).catch(() => {
        // 超时不报错，部分网站用自定义播放器没有标准 video 元素
      });

      const state: Record<string, unknown> = await p.evaluate(injectStateSnapshot()) as any;
      return {
        content: [{ type: "text" as const, text: JSON.stringify({ success: true, ...state }, null, 2) }],
      };
    } catch (error: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ success: false, error: error.message }) }], isError: true };
    }
  }
);

// ── 工具 2: video_play ─────────────────────────────────
server.tool(
  "video_play",
  "播放视频。多策略尝试（video.play() + 点击常见播放按钮）。",
  {},
  async () => {
    try {
      const p = await getPage();
      const result = await p.evaluate(() => {
        const videos = document.querySelectorAll("video");
        let playing = 0;
        for (const v of videos) {
          if (!v.paused && !v.ended) playing++;
          else if (v.paused || v.ended) { v.play().catch(() => {}); playing++; }
        }
        if (playing > 0) return { method: "video.play()", videoCount: videos.length, playing };
        // fallback: 点击播放按钮
        const sels = ['[aria-label="播放"]','[aria-label="Play"]','.play-button','.play-btn','[class*="play"]','.vjs-big-play-button','.ytp-play-button','.bpx-player-ctrl-play','button[title*="播放"]','button[title*="Play"]','[data-action="play"]'];
        for (const sel of sels) {
          const el = document.querySelector(sel) as HTMLElement;
          if (el && el.offsetParent !== null) { el.click(); return { method: `click("${sel}")` }; }
        }
        return null;
      });
      if (result) {
        return { content: [{ type: "text" as const, text: JSON.stringify({ success: true, ...result }) }] };
      }
      return { content: [{ type: "text" as const, text: JSON.stringify({ success: false, error: "no play method found" }) }], isError: true };
    } catch (error: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ success: false, error: error.message }) }], isError: true };
    }
  }
);

// ── 工具 3: video_pause ────────────────────────────────
server.tool(
  "video_pause",
  "暂停视频。",
  {},
  async () => {
    try {
      const p = await getPage();
      const n = await p.evaluate(() => { let c = 0; document.querySelectorAll("video").forEach(v => { if (!v.paused) { v.pause(); c++; } }); return c; });
      return { content: [{ type: "text" as const, text: JSON.stringify({ success: true, pausedCount: n }) }] };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ success: false, error: e.message }) }], isError: true };
    }
  }
);

// ── 工具 4: video_seek — 精准跳转（带等待确认）─────────
server.tool(
  "video_seek",
  `跳转到指定时间并等待帧就绪。
内部流程：设置 currentTime → 等 seeked 事件 → 轮询验证 currentTime → 等 canplay → 返回确认。
返回 {ok, currentTime, drift, readyState, bufferedEnd}，Agent 可根据 drift 决定重试。`,
  {
    seconds: z.number().min(0).describe("目标时间（秒）"),
    tolerance: z.number().default(0.3).describe("允许的时间误差（秒），默认 0.3s"),
    maxWaitMs: z.number().default(10000).describe("最大等待时间（毫秒），默认 10s"),
  },
  async ({ seconds, tolerance, maxWaitMs }) => {
    try {
      const p = await getPage();
      const result = await p.evaluate(injectSeekWaitScript(seconds, tolerance, maxWaitMs));
      return {
        content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }],
      };
    } catch (error: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ success: false, error: error.message }) }], isError: true };
    }
  }
);

// ── 工具 5: video_get_state — 完整状态快照 ─────────────
server.tool(
  "video_get_state",
  "获取视频完整状态：播放位置、缓冲范围、就绪状态、网络状态等。用于 Agent 做决策依据。",
  {},
  async () => {
    try {
      const p = await getPage();
      const state = await p.evaluate(injectStateSnapshot());
      return { content: [{ type: "text" as const, text: JSON.stringify(state, null, 2) }] };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ success: false, error: e.message }) }], isError: true };
    }
  }
);

// ── 工具 6: video_screenshot — 截图（带帧时间标记）─────
server.tool(
  "video_screenshot",
  `截图当前画面，返回 base64 图片 + 截图时的精确视频时间戳。
Agent 可据此验证截图帧是否在预期位置，drift 过大则重试。`,
  {
    name: z.string().optional().describe("自定义文件名（不含扩展名），Agent 根据语义命名，如 '10s_ROS架构对比'"),
    selector: z.string().optional().describe("CSS 选择器，只截取该元素区域"),
    fullPage: z.boolean().default(false),
  },
  async ({ name, selector, fullPage }) => {
    try {
      const p = await getPage();
      ensureScreenshotDir();
      const ts = Date.now();
      const safe = name ? name.replace(/[<>:"/\\|?*]/g, '_').slice(0, 80) : null;
      const filename = safe ? `${safe}.png` : `frame_${ts}.png`;
      const filepath = path.join(SCREENSHOT_DIR, filename);

      if (selector) {
        const el = await p.$(selector);
        if (!el) return { content: [{ type: "text" as const, text: JSON.stringify({ ok: false, error: `selector "${selector}" not found` }) }], isError: true };
        await el.screenshot({ path: filepath, type: "png" });
      } else {
        await p.screenshot({ path: filepath, type: "png", fullPage });
      }

      // 同时获取当前视频时间（截图时的精确状态）
      const vidState: any = await p.evaluate(injectStateSnapshot());
      const buffer = readFileSync(filepath);
      const base64 = buffer.toString("base64");

      return {
        content: [
          { type: "image" as const, data: base64, mimeType: "image/png" },
          {
            type: "text" as const,
            text: JSON.stringify({
              ok: true,
              filepath,
              sizeBytes: buffer.length,
              screenshotTimestamp: new Date(ts).toISOString(),
              videoTime: vidState.hasVideo ? vidState.videos[0].currentTime : null,
              videoDuration: vidState.hasVideo ? vidState.videos[0].duration : null,
              videoReadyState: vidState.hasVideo ? vidState.videos[0].readyStateName : null,
            }, null, 2),
          },
        ],
      };
    } catch (error: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ ok: false, error: error.message }) }], isError: true };
    }
  }
);

// ── 工具 7: video_capture_at — 一键精确截图（核心组合工具）─
server.tool(
  "video_capture_at",
  `【核心工具】跳转到指定时间 → 等待帧解码就绪 → 截图 → 返回图片+精确时间戳。
这是"seek + wait + verify + screenshot"的原子化组合，一条指令完成完整闭环。
返回 base64 图片 + 元数据 {targetTime, actualTime, drift, readyState}，drift<0.5 为可靠。`,
  {
    seconds: z.number().min(0).describe("目标截图时间（秒）"),
    name: z.string().optional().describe("自定义文件名（不含扩展名），Agent 根据语义命名"),
    tolerance: z.number().default(0.5).describe("允许的最大 drift（秒），超出则标记 unreliable"),
    maxWaitMs: z.number().default(15000).describe("最大等待时间（ms），含缓冲时间"),
  },
  async ({ seconds, name, tolerance, maxWaitMs }) => {
    try {
      const p = await getPage();

      // Step 1: 执行精准 seek
      const seekResult = await p.evaluate(injectSeekWaitScript(seconds, 0.3, maxWaitMs * 0.7));

      // Step 2: 截图
      ensureScreenshotDir();
      const ts = Date.now();
      const safe = name ? name.replace(/[<>:"/\\|?*]/g, '_').slice(0, 80) : null;
      const filename = safe ? `${safe}.png` : `capture_${seconds.toFixed(1)}s_${ts}.png`;
      const filepath = path.join(SCREENSHOT_DIR, filename);
      await p.screenshot({ path: filepath, type: "png" });

      // Step 3: 再次确认截图后的实际时间
      const actualTime = await p.evaluate(() => {
        const v = document.querySelector("video");
        return v ? v.currentTime : -1;
      });

      const buffer = readFileSync(filepath);
      const base64 = buffer.toString("base64");
      const drift = Math.abs(actualTime - seconds);
      const reliable = drift <= tolerance && (seekResult as any).ok !== false;

      return {
        content: [
          { type: "image" as const, data: base64, mimeType: "image/png" },
          {
            type: "text" as const,
            text: JSON.stringify({
              ok: reliable,
              filepath,
              sizeBytes: buffer.length,
              targetTime: seconds,
              actualTime,
              drift,
              tolerance,
              reliable,
              readyState: (seekResult as any).readyState,
              seekMethod: (seekResult as any).method,
              hint: !reliable
                ? `drift=${drift.toFixed(2)}s > tolerance=${tolerance}s. Agent should retry with adjusted target or wait longer.`
                : "frame captured reliably",
            }, null, 2),
          },
        ],
      };
    } catch (error: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ ok: false, error: error.message }) }], isError: true };
    }
  }
);

// ── 工具 8: video_capture_batch — 批量截图 + 自动回位（Agent 专用）─
server.tool(
  "video_capture_batch",
  `【Agent 专用批量工具】一次 MCP 调用完成多个截图 + 完成后自动回到原播放位置。

设计目的：
- Agent 一次决策可能产生多个截图需求，逐个调用 video_capture_at 有 N 次 MCP 通信开销
- 本工具一次提交所有截图计划，内部批量执行，省 token、省延迟
- 截图完成后自动 seek 回原播放位置，Agent 不丢进度、不会重复截图

使用方式: Agent 调用 agent_decide_screenshots 得到 [{time, label}] → 直接传入本工具`,
  {
    shots: z.array(z.object({
      time: z.number().min(0).describe("目标截图时间（秒）"),
      name: z.string().describe("语义标签，用于文件命名"),
    })).min(1).max(12).describe("截图计划列表，按时间排序后批量执行。最多 12 个截图点"),
    tolerance: z.number().default(0.5).describe("允许的最大 drift（秒）"),
    maxWaitMs: z.number().default(10000).describe("每帧最大等待时间（ms）"),
  },
  async ({ shots, tolerance, maxWaitMs }) => {
    try {
      const p = await getPage();

      // ▸ 保存当前播放状态
      const savedState = await p.evaluate(() => {
        const v = document.querySelector("video");
        return v ? { currentTime: v.currentTime, paused: v.paused } : null;
      });

      if (!savedState) {
        return { content: [{ type: "text" as const, text: JSON.stringify({ ok: false, error: "no video found" }) }], isError: true };
      }

      const results: any[] = [];

      // ▸ 按时间排序（避免视频跳来跳去造成解码压力）
      const sorted = [...shots].sort((a, b) => a.time - b.time);
      for (const shot of sorted) {
        try {
          // 精准 seek + 等待帧就绪
          const seekResult = await p.evaluate(injectSeekWaitScript(shot.time, 0.3, maxWaitMs * 0.7));

          // 截图
          ensureScreenshotDir();
          const safe = shot.name.replace(/[<>:"/\\|?*]/g, "_").slice(0, 80);
          const filename = `${safe}.png`;
          const filepath = path.join(SCREENSHOT_DIR, filename);
          await p.screenshot({ path: filepath, type: "png" });

          const actualTime = await p.evaluate(() => {
            const v = document.querySelector("video");
            return v ? v.currentTime : -1;
          });

          const buffer = readFileSync(filepath);
          const drift = Math.abs(actualTime - shot.time);
          const reliable = drift <= tolerance && (seekResult as any).ok !== false;

          results.push({
            targetTime: shot.time,
            name: shot.name,
            actualTime,
            drift,
            reliable,
            filepath,
            sizeBytes: buffer.length,
            base64: buffer.toString("base64"),
          });
        } catch (e: any) {
          results.push({ targetTime: shot.time, name: shot.name, error: e.message });
        }
      }

      // ▸ 回到原播放位置（自动回位，Agent 不丢进度）
      await p.evaluate(injectSeekWaitScript(savedState.currentTime, 0.3, 8000));
      if (!savedState.paused) {
        await p.evaluate(() => {
          const v = document.querySelector("video");
          if (v && v.paused) (v as HTMLVideoElement).play().catch(() => {});
        });
      }

      // ▸ 验证回到原位
      const finalTime = await p.evaluate(() => {
        const v = document.querySelector("video");
        return v ? v.currentTime : -1;
      });

      const captured = results.filter(r => !r.error);
      const failed = results.filter(r => r.error);

      return {
        content: [
          ...captured.map(r => ({
            type: "image" as const,
            data: r.base64,
            mimeType: "image/png" as const,
          })),
          {
            type: "text" as const,
            text: JSON.stringify({
              ok: true,
              totalShots: shots.length,
              captured: captured.length,
              failed: failed.length,
              savedPosition: savedState.currentTime,
              returnedTo: finalTime,
              returnDrift: Math.abs(finalTime - savedState.currentTime),
              resumePlay: !savedState.paused,
              results: results.map(r => ({
                targetTime: r.targetTime,
                name: r.name,
                drift: r.drift ?? null,
                reliable: r.reliable ?? false,
                filepath: r.filepath ?? null,
                error: r.error ?? null,
              })),
            }, null, 2),
          },
        ],
      };
    } catch (error: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ ok: false, error: error.message }) }], isError: true };
    }
  }
);

// ── 工具 9: video_capture_sequence — 等间隔批量截图（滑动窗口采样）─
server.tool(
  "video_capture_sequence",
  `【批量工具】在时间区间内等间隔截取多帧。类似 live_caption 滑动窗口思路：
对 [start, end] 每隔 interval 秒截一帧，每帧都带时间戳验证。
返回每帧的 base64 图片 + drift 元数据。Agent 可据此做帧级视觉分析。`,
  {
    startSeconds: z.number().min(0).describe("起始时间（秒）"),
    endSeconds: z.number().min(0).describe("结束时间（秒）"),
    intervalSeconds: z.number().default(5).describe("截图间隔（秒），默认每 5 秒一帧"),
    maxFrames: z.number().default(20).describe("最大帧数限制"),
  },
  async ({ startSeconds, endSeconds, intervalSeconds, maxFrames }) => {
    try {
      const p = await getPage();
      const frames: any[] = [];
      let t = startSeconds;
      let count = 0;

      while (t <= endSeconds && count < maxFrames) {
        // seek + wait
        await p.evaluate(injectSeekWaitScript(t, 0.3, 8000));

        // 截图
        ensureScreenshotDir();
        const filename = `seq_${t.toFixed(1)}s_${Date.now()}.png`;
        const filepath = path.join(SCREENSHOT_DIR, filename);
        await p.screenshot({ path: filepath, type: "png" });

        const actualTime = await p.evaluate(() => {
          const v = document.querySelector("video"); return v ? v.currentTime : -1;
        });

        const buffer = readFileSync(filepath);
        frames.push({
          targetTime: t,
          actualTime,
          drift: Math.abs(actualTime - t),
          base64: buffer.toString("base64"),
          filepath,
          sizeBytes: buffer.length,
        });

        t += intervalSeconds;
        count++;
      }

      return {
        content: [
          ...frames.map(f => ({ type: "image" as const, data: f.base64, mimeType: "image/png" })),
          {
            type: "text" as const,
            text: JSON.stringify({
              ok: true,
              totalFrames: frames.length,
              range: [startSeconds, endSeconds],
              summary: frames.map(f => ({ targetTime: f.targetTime, actualTime: f.actualTime, drift: f.drift, filepath: f.filepath })),
            }, null, 2),
          },
        ],
      };
    } catch (error: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ ok: false, error: error.message }) }], isError: true };
    }
  }
);

// ── 工具 9: video_exec_js ──────────────────────────────
server.tool(
  "video_exec_js",
  "在视频页面执行自定义 JS，用于操控特殊播放器。可使用 return 返回结果。",
  { code: z.string().describe("JS 代码") },
  async ({ code }) => {
    try {
      const p = await getPage();
      const r = await p.evaluate((js) => {
        try { const fn = new Function(`"use strict"; return (${js})`); return { ok: true, result: fn() }; }
        catch (e: any) { return { ok: false, error: e.message }; }
      }, code);
      return { content: [{ type: "text" as const, text: JSON.stringify(r, null, 2) }] };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ ok: false, error: e.message }) }], isError: true };
    }
  }
);

// ── 工具 10: video_close ───────────────────────────────
server.tool(
  "video_close",
  "关闭浏览器。",
  {},
  async () => {
    try {
      if (page) { await page.close().catch(() => {}); page = null; }
      if (context) { await context.close().catch(() => {}); context = null; }
      if (browser) { await browser.close().catch(() => {}); browser = null; }
      return { content: [{ type: "text" as const, text: JSON.stringify({ ok: true, message: "browser closed" }) }] };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: JSON.stringify({ ok: false, error: e.message }) }], isError: true };
    }
  }
);

// ── 启动 ───────────────────────────────────────────────
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[mcp-video-agent] v2.0.0 — 可靠视频截图模式已启动");
}

main().catch((err) => {
  console.error("[mcp-video-agent] 启动失败:", err);
  process.exit(1);
});
