/**
 * 交互式测试：在一个 MCP 会话中连续测 navigate → state → pause → state → play → state
 */
import { spawn } from "child_process";
import { createInterface } from "readline";
import * as fs from "fs";

const server = spawn("node", ["dist/index.js"], { cwd: "g:/auto learn plan" });
const rl = createInterface({ input: server.stdout });
server.stderr.pipe(fs.createWriteStream("g:/auto learn plan/stderr.log"));

let id = 0;
const pending = new Map();
const results = [];

function send(method, params = {}) {
  const msg = { jsonrpc: "2.0", method, params, id: ++id };
  pending.set(id, { method, params });
  console.log(`>>> ${method}`);
  server.stdin.write(JSON.stringify(msg) + "\n");
}

rl.on("line", (line) => {
  const resp = JSON.parse(line);
  if (resp.id && pending.has(resp.id)) {
    const req = pending.get(resp.id);
    pending.delete(resp.id);
    const text = resp.result?.content?.[0]?.text;
    if (text) {
      try {
        const data = JSON.parse(text);
        results.push({ method: req.method, data });
        if (data.success !== undefined) {
          const status = data.success ? "OK" : "FAIL";
          if (req.method === "video_get_state") {
            console.log(`<<< ${req.method}: ${status}`);
            if (data.videos?.length) {
              const v = data.videos[0];
              console.log(`    paused=${v.paused}  time=${v.currentTime?.toFixed(1)}s  duration=${v.duration}s  readyState=${v.readyState}`);
            } else {
              console.log(`    hasVideo=false`);
            }
          } else {
            console.log(`<<< ${req.method}: ${status}  ${JSON.stringify(data).slice(0, 120)}`);
          }
        }
      } catch { console.log(`<<< ${req.method}: ${text.slice(0, 150)}`); }
    }
  }
});

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function main() {
  send("initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "test", version: "1.0" } });
  await sleep(200);
  send("notifications/initialized");
  await sleep(200);

  // Step 1: 打开视频
  send("tools/call", { name: "video_navigate", arguments: { url: "https://www.bilibili.com/video/BV1GJ411x7h7" } });
  await sleep(10000); // 给浏览器时间启动 + 页面加载 + 播放器初始化

  // Step 2: 查看状态（应该是自动播放中）
  send("tools/call", { name: "video_get_state", arguments: {} });
  await sleep(500);

  // Step 3: 暂停
  send("tools/call", { name: "video_pause", arguments: {} });
  await sleep(500);

  // Step 4: 查看状态（应该暂停了）
  send("tools/call", { name: "video_get_state", arguments: {} });
  await sleep(500);

  // Step 5: 播放
  send("tools/call", { name: "video_play", arguments: {} });
  await sleep(1500);

  // Step 6: 查看状态（应该播放中）
  send("tools/call", { name: "video_get_state", arguments: {} });
  await sleep(500);

  console.log("\n" + "=".repeat(55));
  console.log("播放/暂停 链路测试完成");
  console.log("=".repeat(55) + "\n");

  server.kill();
  process.exit(0);
}

main().catch(e => { console.error(e); server.kill(); process.exit(1); });
