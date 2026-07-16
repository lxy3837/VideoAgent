// 测试：seek 跳转进度条
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

      if (data.videos) {
        const v = data.videos[0];
        if (v) console.log(`  → paused=${v.paused}  time=${v.currentTime?.toFixed(1)}s  dur=${v.duration}s  ready=${v.readyStateName || v.readyState}`);
        else console.log("  → hasVideo=false");
      } else {
        const s = JSON.stringify(data);
        console.log(`  → ${s.slice(0, 200)}`);
      }
    } catch {}
  }
});

setTimeout(() => send("initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "t", version: "1" } }), 100);
setTimeout(() => send("notifications/initialized"), 300);
setTimeout(() => send("tools/call", { name: "video_navigate", arguments: { url: "https://www.bilibili.com/video/BV1GJ411x7h7" } }), 500);

// 先看看初始状态，然后暂停
setTimeout(() => send("tools/call", { name: "video_pause", arguments: {} }), 9000);
setTimeout(() => send("tools/call", { name: "video_get_state", arguments: {} }), 10000);

// seek 到 60 秒
setTimeout(() => { console.log("\n--- SEEKing to 60s ---"); send("tools/call", { name: "video_seek", arguments: { seconds: 60 } }); }, 10500);
setTimeout(() => send("tools/call", { name: "video_get_state", arguments: {} }), 11500);

// seek 到 120 秒
setTimeout(() => { console.log("\n--- SEEKing to 120s ---"); send("tools/call", { name: "video_seek", arguments: { seconds: 120 } }); }, 12000);
setTimeout(() => send("tools/call", { name: "video_get_state", arguments: {} }), 13000);

// seek 回 30 秒
setTimeout(() => { console.log("\n--- SEEKing back to 30s ---"); send("tools/call", { name: "video_seek", arguments: { seconds: 30 } }); }, 13500);
setTimeout(() => send("tools/call", { name: "video_get_state", arguments: {} }), 14500);

setTimeout(() => { console.log("\nDONE."); s.kill(); process.exit(0); }, 16000);
