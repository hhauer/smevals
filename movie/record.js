#!/usr/bin/env node
// record.js - drives a real browser against a running smevals studio and
// captures each scene as a silent video clip (or, for title scenes, a still
// PNG). See README.md for the full pipeline and scenes.yaml for the schema.
//
// Capture strategy: CDP Page.startScreencast, not Playwright's built-in
// context.recordVideo(). recordVideo needs Playwright's own bundled ffmpeg
// binary (a separate download keyed to the Playwright version, cached under
// ~/Library/Caches/ms-playwright) which isn't present and which the task
// asked us not to fetch. Raw Page.startScreencast frames + the system
// ffmpeg (already on PATH) avoid that dependency entirely, and as a bonus
// give frame-accurate timestamps: Chrome only emits a screencast frame when
// the page actually repaints, so a `ffmpeg -f concat` file built from real
// inter-frame deltas reconstructs true wall-clock pacing instead of forcing
// a fixed frame rate.
"use strict";
const fs = require("fs");
const path = require("path");
const http = require("http");
const { spawn, execFileSync } = require("child_process");
const { chromium } = require("playwright-core");
const { loadScenes } = require("./scenes.js");

const CURSOR_OVERLAY_SRC = require("./cursor-overlay.js");
const TITLECARD_TEMPLATE = fs.readFileSync(
  path.join(__dirname, "titlecard.html"),
  "utf8"
);

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) {
        args[key] = true;
      } else {
        args[key] = next;
        i++;
      }
    } else {
      args._.push(a);
    }
  }
  return args;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function httpGetOk(url) {
  return new Promise(resolve => {
    const req = http.get(url, res => {
      res.resume();
      resolve(res.statusCode >= 200 && res.statusCode < 500);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(2000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForServer(baseUrl, { tries = 30, intervalMs = 500 } = {}) {
  for (let i = 0; i < tries; i++) {
    if (await httpGetOk(baseUrl)) return true;
    await sleep(intervalMs);
  }
  return false;
}

// Launches `uv run smevals studio <serveDir> -p <port>` from repoDir, as the
// task's toolchain step (1) requires, and waits until it answers HTTP.
function startStudio({ repoDir, serveDir, port }) {
  const baseUrl = `http://127.0.0.1:${port}`;
  console.error(`[record] launching: uv run smevals studio ${serveDir} -p ${port} (cwd=${repoDir})`);
  const child = spawn("uv", ["run", "smevals", "studio", serveDir, "-p", String(port)], {
    cwd: repoDir,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let out = "";
  let stopping = false;
  child.stdout.on("data", d => (out += d));
  child.stderr.on("data", d => (out += d));
  child.on("exit", code => {
    if (!stopping && code !== null && code !== 0) {
      console.error(`[record] studio server exited early (code ${code}):\n${out}`);
    }
  });
  return {
    baseUrl,
    async ready() {
      const ok = await waitForServer(baseUrl);
      if (!ok) {
        throw new Error(
          `studio server at ${baseUrl} never came up. Server output:\n${out}`
        );
      }
    },
    // cli.py's `studio` command prints "...on http://127.0.0.1:PORT/?k=TOKEN"
    // to stdout before it starts serving (see cli.py's `studio` command) -
    // every /api/* request needs that token as an X-Studio-Key header, and
    // studio.html only ever picks it up from a `?k=` URL query param (see
    // studio.html's "studio key" section). By the time ready() has resolved
    // the server already answered an HTTP request, so the startup line is
    // guaranteed to already be in `out`.
    getToken() {
      const m = out.match(/\?k=([A-Za-z0-9_-]+)/);
      return m ? m[1] : null;
    },
    stop() {
      if (child.exitCode !== null) return;
      stopping = true;
      child.kill("SIGTERM");
      setTimeout(() => {
        if (child.exitCode === null) child.kill("SIGKILL");
      }, 3000);
    },
  };
}

// ---- cursor motion / human-ish input pacing --------------------------------

function easeOutCubic(t) {
  return 1 - Math.pow(1 - t, 3);
}

async function smoothMoveTo(page, cursorState, targetX, targetY, opts = {}) {
  const steps = opts.steps ?? 24;
  const stepDelayMs = opts.stepDelayMs ?? 12;
  const { x: startX, y: startY } = cursorState;
  for (let i = 1; i <= steps; i++) {
    const e = easeOutCubic(i / steps);
    const x = startX + (targetX - startX) * e;
    const y = startY + (targetY - startY) * e;
    await page.mouse.move(x, y);
    await sleep(stepDelayMs);
  }
  cursorState.x = targetX;
  cursorState.y = targetY;
}

async function humanClick(page, cursorState, selector) {
  const locator = page.locator(selector).first();
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error(`click: no visible element for selector ${selector}`);
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await smoothMoveTo(page, cursorState, x, y);
  await page.mouse.down();
  await sleep(70 + Math.random() * 60);
  await page.mouse.up();
}

async function humanType(page, cursorState, selector, text) {
  const locator = page.locator(selector).first();
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error(`type: no visible element for selector ${selector}`);
  await smoothMoveTo(page, cursorState, box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await sleep(60);
  await page.mouse.up();
  for (const ch of text) {
    await page.keyboard.type(ch);
    const base = /[.,!?]/.test(ch) ? 180 : /\s/.test(ch) ? 130 : 55;
    await sleep(base + Math.random() * 90);
  }
}

// ---- one "wait_for"/"pause"/"goto" action dispatch -------------------------

async function runAction(page, cursorState, baseUrl, action, navState, token) {
  if ("goto" in action) {
    const url = new URL(action.goto, baseUrl);
    // Studio gates every /api/* call behind X-Studio-Key; studio.html only
    // ever learns the token from a `?k=` query param on page load (it
    // stashes it in sessionStorage, then scrubs the URL). Each scene is a
    // fresh browser context (fresh, empty storage), so the token has to
    // ride the FIRST navigation of the scene - every navigation after that
    // is either the same tab's sessionStorage still holding it, or (for a
    // same-pathname hash-only goto) not even a new document load at all.
    // Tagging every goto with `?k=` would defeat that: studio.html scrubs
    // `k` from the visible URL after the first load, so a later goto that
    // still carries it would no longer match the current document's query
    // string and would force a full reload instead of a same-document hash
    // change.
    if (token && navState && !navState.tokenApplied) {
      url.searchParams.set("k", token);
      navState.tokenApplied = true;
    }
    await page.goto(url.toString(), { waitUntil: "load", timeout: 15000 });
    return;
  }
  if ("wait_for" in action) {
    const spec = action.wait_for;
    const selector = typeof spec === "string" ? spec : spec.selector;
    const timeout = typeof spec === "string" ? 10000 : spec.timeout || 10000;
    await page.waitForSelector(selector, { state: "visible", timeout });
    return;
  }
  if ("click" in action) {
    await humanClick(page, cursorState, action.click);
    return;
  }
  if ("type" in action) {
    const { selector, text } = action.type;
    await humanType(page, cursorState, selector, text);
    return;
  }
  if ("pause" in action) {
    await sleep(action.pause);
    return;
  }
  if ("append" in action) {
    // click into the field, drive the caret to the very end, then type
    // with the same human pacing as "type" - for editing an existing
    // value without replacing it (v2's prompt-edit beat)
    const { selector, text } = action.append;
    await humanClick(page, cursorState, selector);
    await page.keyboard.press("ControlOrMeta+ArrowDown");
    await sleep(250);
    for (const ch of text) {
      await page.keyboard.type(ch);
      const base = /[.,!?]/.test(ch) ? 180 : /\s/.test(ch) ? 130 : 55;
      await sleep(base + Math.random() * 90);
    }
    return;
  }
  if ("select" in action) {
    // native <select>: click for the camera, then selectOption (which
    // fires the change event the app listens for)
    const { selector, value } = action.select;
    await humanClick(page, cursorState, selector);
    await sleep(250);
    await page.selectOption(selector, value);
    await sleep(150);
    return;
  }
  throw new Error(`unknown action: ${JSON.stringify(action)}`);
}

// ---- CDP screencast capture -> ffmpeg concat-demuxer clip ------------------

async function captureBrowserScene(browser, scene, plan, baseUrl, outDir, keepFrames, token) {
  const { width, height } = plan.resolution;
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 1,
  });
  await context.addInitScript(CURSOR_OVERLAY_SRC);
  const page = await context.newPage();
  const cursorState = { x: width / 2, y: height / 2 };
  await page.mouse.move(cursorState.x, cursorState.y);

  const cdp = await context.newCDPSession(page);
  const framesDir = path.join(outDir, "frames", scene.id);
  fs.rmSync(framesDir, { recursive: true, force: true });
  fs.mkdirSync(framesDir, { recursive: true });

  let frameCount = 0;
  const manifest = [];
  const t0 = Date.now();
  cdp.on("Page.screencastFrame", async frame => {
    const ts = Date.now() - t0;
    const fname = `frame_${String(frameCount).padStart(6, "0")}.png`;
    fs.writeFileSync(path.join(framesDir, fname), Buffer.from(frame.data, "base64"));
    manifest.push({ fname, ts });
    frameCount++;
    try {
      await cdp.send("Page.screencastFrameAck", { sessionId: frame.sessionId });
    } catch {
      /* session may already be closing */
    }
  });

  await cdp.send("Page.startScreencast", { format: "png", everyNthFrame: 1 });

  console.error(`[record] scene "${scene.id}": running ${scene.actions.length} action(s)`);
  const navState = { tokenApplied: false };
  for (const action of scene.actions) {
    await runAction(page, cursorState, baseUrl, action, navState, token);
    if (process.env.RECORD_DEBUG_TIMING) {
      console.error(`  [debug] t=${Date.now() - t0}ms after ${JSON.stringify(action)}`);
    }
  }
  const actionsEndTs = Date.now() - t0;

  await cdp.send("Page.stopScreencast");
  await sleep(200); // let trailing frame acks land
  await context.close();

  if (manifest.length === 0) {
    throw new Error(`scene "${scene.id}" captured zero frames`);
  }
  fs.writeFileSync(
    path.join(framesDir, "manifest.json"),
    JSON.stringify(manifest, null, 2)
  );

  // Chrome only emits a screencast frame on repaint, so a trailing "pause"
  // action with nothing animating produces no frames at all: the last
  // frame's on-screen hold time is NOT 0, it's everything from its own
  // timestamp through the end of the actions loop (actionsEndTs). Getting
  // this wrong silently truncates whatever the scene was holding on when
  // it ends - exactly the final beat a scene is usually built to land on.
  const concatPath = path.join(framesDir, "concat.txt");
  const lines = [];
  for (let i = 0; i < manifest.length; i++) {
    const cur = manifest[i];
    const next = manifest[i + 1];
    const nextTs = next ? next.ts : Math.max(actionsEndTs, cur.ts + 1);
    const dur = Math.max((nextTs - cur.ts) / 1000, 1 / 60);
    lines.push(`file '${cur.fname}'`);
    lines.push(`duration ${dur.toFixed(3)}`);
  }
  lines.push(`file '${manifest[manifest.length - 1].fname}'`); // concat quirk
  fs.writeFileSync(concatPath, lines.join("\n"));

  const clipsDir = path.join(outDir, "clips");
  fs.mkdirSync(clipsDir, { recursive: true });
  const clipPath = path.join(clipsDir, `${scene.id}.mp4`);
  execFileSync("ffmpeg", [
    "-nostdin", "-y",
    "-f", "concat", "-safe", "0", "-i", concatPath,
    "-vsync", "vfr",
    "-pix_fmt", "yuv420p",
    // -bf 0: disable B-frames. x264's B-frame reordering silently DROPS
    // frames when the concat-vfr stream has a huge gap between two frame
    // timestamps (exactly what a long trailing `pause` produces) - found
    // by bisecting a 23.8s scene that was encoding to 15.2s with no error
    // or warning from ffmpeg. -preset medium is otherwise unaffected.
    "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-bf", "0",
    clipPath,
  ], { stdio: ["ignore", "ignore", "inherit"] });

  console.error(
    `[record] scene "${scene.id}": ${frameCount} frames -> ${path.relative(process.cwd(), clipPath)}`
  );

  if (!keepFrames) fs.rmSync(framesDir, { recursive: true, force: true });
}

// ---- title-card scene: render HTML, screenshot to PNG ----------------------

async function captureTitleScene(browser, scene, plan, outDir) {
  const { width, height } = plan.resolution;
  const html = TITLECARD_TEMPLATE
    .replace("{{TITLE}}", escapeHtml(scene.title || ""))
    .replace("{{SUBTITLE}}", escapeHtml(scene.subtitle || ""));
  const tmpHtml = path.join(outDir, "titles", `${scene.id}.html`);
  fs.mkdirSync(path.dirname(tmpHtml), { recursive: true });
  fs.writeFileSync(tmpHtml, html);

  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  await page.goto("file://" + tmpHtml);
  const pngPath = path.join(outDir, "titles", `${scene.id}.png`);
  await page.screenshot({ path: pngPath });
  await context.close();
  console.error(`[record] scene "${scene.id}": title card -> ${path.relative(process.cwd(), pngPath)}`);
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---- main -------------------------------------------------------------------

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const scenesPath = args._[0];
  if (!scenesPath) {
    console.error(
      "usage: node record.js <scenes.yaml> --out <dir> " +
        "[--repo-dir DIR --serve-dir DIR --port N | --base-url URL [--token TOKEN]] " +
        "[--headed] [--keep-frames]"
    );
    process.exit(1);
  }
  const outDir = path.resolve(args.out || "work");
  fs.mkdirSync(outDir, { recursive: true });

  const plan = loadScenes(scenesPath);

  let server = null;
  let baseUrl = args["base-url"] || plan.base_url;
  if (args["serve-dir"]) {
    if (!args["repo-dir"] || !args.port) {
      throw new Error("--serve-dir requires --repo-dir and --port");
    }
    server = startStudio({
      repoDir: path.resolve(args["repo-dir"]),
      serveDir: args["serve-dir"],
      port: args.port,
    });
    baseUrl = server.baseUrl;
  }
  if (!baseUrl) throw new Error("no base URL: pass --base-url or --serve-dir/--repo-dir/--port");

  // Studio gates /api/* behind X-Studio-Key (see cli.py's `studio` command /
  // studio.py). When record.js launches the server itself, the token comes
  // off that server's own stdout (see startStudio().getToken()). When
  // pointing at an already-running studio via --base-url, there's no stdout
  // to read - pass --token explicitly (copy it from the `?k=` query param
  // in that server's own startup line).
  let token = args.token || null;

  const browser = await chromium.launch({
    channel: "chrome",
    headless: !args.headed,
  });

  try {
    if (server) {
      console.error(`[record] waiting for ${baseUrl} ...`);
      await server.ready();
      if (!token) token = server.getToken();
      if (!token) {
        console.error("[record] WARNING: no ?k= token found in studio's startup output - /api/* calls will 403");
      }
    }
    for (const scene of plan.scenes) {
      if (scene.type === "title") {
        await captureTitleScene(browser, scene, plan, outDir);
      } else {
        await captureBrowserScene(browser, scene, plan, baseUrl, outDir, !!args["keep-frames"], token);
      }
    }
  } finally {
    await browser.close();
    if (server) {
      console.error("[record] stopping studio server");
      server.stop();
    }
  }
  console.error("[record] done");
}

main().catch(ex => {
  console.error("[record] FAILED:", ex);
  process.exit(1);
});
