// 快速测试：navigate → state → pause → state
const { spawn } = require("child_process");

const s = spawn("node", ["dist/index.js"], { cwd: "g:/auto learn plan" });
let id = 0;

function send(method, params) {
  const msg = JSON.stringify({ jsonrpc: "2.0", method, params: params || {}, id: ++id });
  console.log(">>>", method.replace("tools/call", params ? params.name : ""));
  s.stdin.write(msg + "\n");
}

s.stdout.on("data", (chunk) => {
  for (const line of chunk.toString().split("\n").filter(Boolean)) {
    try {
      const r = JSON.parse(line);
      if (!r.result) continue;
      const text = r.result?.content?.[0]?.text;
      if (!text) continue;
      const data = JSON.parse(text);

      if (data.hasVideo !== undefined) {
        const v = data.videos?.[0];
        if (v) console.log(`  → paused=${v.paused}  time=${v.currentTime?.toFixed(1)}s  dur=${v.duration}s  readyState=${v.readyState}`);
        else console.log(`  → hasVideo=false`);
      } else if (data.success !== undefined) {
        console.log(`  → ${data.success ? "OK" : "FAIL"}`, JSON.stringify(data).slice(0, 120));
      }
    } catch {}
  }
});

setTimeout(() => send("initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "t", version: "1" } }), 100);
setTimeout(() => send("notifications/initialized"), 300);
setTimeout(() => send("tools/call", { name: "video_navigate", arguments: { url: "https://www.bilibili.com/video/BV1GJ411x7h7" } }), 500);
setTimeout(() => send("tools/call", { name: "video_get_state", arguments: {} }), 12000);
setTimeout(() => send("tools/call", { name: "video_pause", arguments: {} }), 13500);
setTimeout(() => send("tools/call", { name: "video_get_state", arguments: {} }), 14500);
setTimeout(() => send("tools/call", { name: "video_play", arguments: {} }), 15500);
setTimeout(() => send("tools/call", { name: "video_get_state", arguments: {} }), 16500);
setTimeout(() => { console.log("\nDONE. 浏览器将自动关闭。"); s.kill(); process.exit(0); }, 18500);
