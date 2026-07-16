// 测试: video_capture_batch — 批量截图 + 自动回位
const { spawn } = require("child_process");
const { readFileSync } = require("fs");
const path = require("path");

const s = spawn("node", ["dist/index.js"], { cwd: "g:/auto learn plan", stdio: ["pipe", "pipe", "pipe"] });

// B站视频
const URL = "https://www.bilibili.com/video/BV1mxb3zKEzL/?spm_id_from=333.337.search-card.all.click&vd_source=65647d080500ee5a6f027fa53c67a0de";
const COOKIES = ""; // 设为你自己的 B站 Cookie，或留空

let id = 0;
let pending = {};
let step = 0;

function send(method, params) {
  const _id = ++id;
  const msg = JSON.stringify({ jsonrpc: "2.0", method, params: params || {}, id: _id });
  pending[_id] = method.replace("tools/call", params?.name || "");
  s.stdin.write(msg + "\n");
}

function now() { return new Date().toTimeString().slice(0,8); }

s.stdout.on("data", (chunk) => {
  for (const line of chunk.toString().split("\n").filter(Boolean)) {
    try {
      const r = JSON.parse(line);
      if (!r.result) continue;
      const pname = pending[r.id] || `id=${r.id}`;
      delete pending[r.id];

      // 图片数据不打印
      const contents = r.result?.content || [];
      let textContent = null;
      let imageCount = 0;
      for (const c of contents) {
        if (c.type === "text") textContent = c.text;
        if (c.type === "image") imageCount++;
      }

      if (textContent) {
        try {
          const data = JSON.parse(textContent);
          console.log(`[${now()}] <== ${pname}`);
          // 提取关键字段
          const keys = Object.keys(data);
          if (keys.includes("captured")) {
            // batch 结果
            console.log(`  总截图: ${data.totalShots}, 成功: ${data.captured}, 失败: ${data.failed}`);
            console.log(`  保存位置: ${data.savedPosition?.toFixed(1)}s`);
            console.log(`  回位: ${data.returnedTo?.toFixed(1)}s, drift: ${data.returnDrift?.toFixed(2)}s`);
            console.log(`  恢复播放: ${data.resumePlay}`);
            if (data.results) {
              for (const shot of data.results) {
                if (shot.error) {
                  console.log(`    ✗ ${shot.name} @ ${shot.targetTime}s: ERROR(${shot.error})`);
                } else {
                  console.log(`    ✓ ${shot.name} @ ${shot.targetTime}s → actual=${shot.actualTime?.toFixed(1)}s drift=${shot.drift?.toFixed(2)}s ${shot.reliable ? "✓" : "⚠"} → ${shot.filepath}`);
                }
              }
            }
          } else if (keys.includes("currentTime")) {
            // state 结果
            console.log(`  time=${data.currentTime?.toFixed(1)}s paused=${data.paused} dur=${data.duration}s ready=${data.readyStateName || data.readyState}`);
          } else if (keys.includes("playing")) {
            // play 结果
            console.log(`  播放中: ${data.playing}台 (共${data.videoCount}台), method=${data.method}`);
          } else {
            console.log("  " + textContent.slice(0, 200));
          }
        } catch {
          console.log("  " + textContent.slice(0, 200));
        }
      }
      if (imageCount) console.log(`  [${imageCount} 张图片]`);

      // ---- 步骤推进 ----
      nextStep();
    } catch {}
  }
});

s.stderr.on("data", (d) => console.error("STDERR:", d.toString()));

function nextStep() {
  step++;
  console.log(`\n--- Step ${step} ---`);

  if (step === 1) {
    console.log("1. Initialize");
    send("initialize", { protocolVersion: "2025-06-18", clientInfo: { name: "test", version: "1.0" }, capabilities: {} });
  } else if (step === 2) {
    console.log("2. Navigate to video");
    send("tools/call", { name: "video_navigate", arguments: { url: URL, waitUntil: "networkidle", timeout: 120000 } });
  } else if (step === 3) {
    console.log("3. Set cookies (登录态)");
    send("tools/call", { name: "video_exec_js", arguments: { code: `document.cookie.split(";").length + " cookies"` } });
  } else if (step === 4) {
    console.log("4. Get video state (pre-play)");
    send("tools/call", { name: "video_get_state", arguments: {} });
  } else if (step === 5) {
    console.log("5. Play video");
    setTimeout(() => {
      send("tools/call", { name: "video_play", arguments: {} });
    }, 1000);
  } else if (step === 6) {
    console.log("6. Wait 5s, then get state (video is playing, note the position)");
    setTimeout(() => {
      send("tools/call", { name: "video_get_state", arguments: {} });
    }, 5000);
  } else if (step === 7) {
    console.log("7. >>> video_capture_batch: 6 shots in one call, auto-return <<<");
    setTimeout(() => {
      send("tools/call", {
        name: "video_capture_batch",
        arguments: {
          shots: [
            { time: 0, name: "开场介绍" },
            { time: 10, name: "期刊背景" },
            { time: 25, name: "影响因子数据" },
            { time: 50, name: "版面费介绍" },
            { time: 80, name: "投稿流程" },
            { time: 100, name: "实战教程" },
          ]
        }
      });
    }, 1000);
  } else if (step === 8) {
    console.log("8. 验证：检查当前 time 是否回到 step6 的位置附近");
    setTimeout(() => {
      send("tools/call", { name: "video_get_state", arguments: {} });
    }, 1000);
  } else if (step === 9) {
    console.log("9. 验证：检查视频是否在继续播放");
    setTimeout(() => {
      send("tools/call", { name: "video_get_state", arguments: {} });
    }, 3000);
  } else if (step >= 10) {
    console.log("\n===== TEST COMPLETE =====");
    setTimeout(() => { s.kill(); process.exit(0); }, 500);
  }
}

// 启动
setTimeout(nextStep, 1000);
console.log("=== video_capture_batch 测试 ===");
console.log("URL:", URL.slice(0, 60) + "...");
