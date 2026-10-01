// Launch Grok Bot against the DevBox plugin/bot desktop backend.
// Starts `python -m devbox_bot.desktop` on 127.0.0.1:7811 (DEVBOX_BOT_DIR
// points at a DevBox plugin/bot checkout), waits for /healthz, then
// launches `Grok Bot.exe` with the backend/website env pointed at the
// local backend. Legacy SAND_HOST_GATEWAY_* env still works as a debug
// fallback — if it is already set we keep it and skip the local backend.
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { windowsOutputApp } from "./lib/config.mjs";

const DESKTOP_PORT = Number(process.env.DEVBOX_BOT_PORT || 7811);
const DESKTOP_URL = `http://127.0.0.1:${DESKTOP_PORT}`;

function parseArgs(argv) {
  const options = { printEnvOnly: false, port: DESKTOP_PORT };
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--bot-dir") options.botDir = argv[++index];
    else if (argv[index] === "--app") options.app = argv[++index];
    else if (argv[index] === "--port") options.port = Number(argv[++index]);
    else if (argv[index] === "--print-env-only") options.printEnvOnly = true;
    else throw new Error(`unknown argument: ${argv[index]}`);
  }
  return options;
}

function findPython() {
  for (const cmd of ["python", "python3", "py"]) {
    const probe = spawnSync(cmd, ["--version"], { stdio: "pipe" });
    if (probe.status === 0) return cmd;
  }
  throw new Error("python not found on PATH (need Python 3.11+)");
}

function findBotDir(option) {
  const candidates = [
    option,
    process.env.DEVBOX_BOT_DIR,
    path.resolve(process.cwd(), "devbox-bot"),
    path.resolve(process.cwd(), "..", "DevBox", "plugin", "bot"),
  ].filter(Boolean);
  for (const dir of candidates) {
    if (fs.existsSync(path.join(dir, "devbox_bot", "desktop.py"))) return dir;
  }
  throw new Error(
    "plugin/bot checkout not found; set DEVBOX_BOT_DIR or --bot-dir");
}

async function waitForHealth(url, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${url}/healthz`);
      if (response.ok) return;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = String(error?.message ?? error);
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`desktop backend did not become healthy: ${lastError}`);
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const port = options.port || DESKTOP_PORT;
  const url = `http://127.0.0.1:${port}`;
  const appExe = options.app ?? path.join(windowsOutputApp, "Grok Bot.exe");

  const env = {};
  if (process.env.SAND_HOST_GATEWAY_URL) {
    // debug fallback: caller pinned an explicit gateway
    env.SAND_HOST_GATEWAY_URL = process.env.SAND_HOST_GATEWAY_URL;
    env.SAND_HOST_GATEWAY_TOKEN = process.env.SAND_HOST_GATEWAY_TOKEN ?? "";
    env.SAND_HOST_GATEWAY_NETWORK_TOKEN =
      process.env.SAND_HOST_GATEWAY_NETWORK_TOKEN ?? "";
    console.log("Using pinned SAND_HOST_GATEWAY_* env (debug fallback)");
  } else {
    const botDir = findBotDir(options.botDir);
    const python = findPython();
    const reqs = path.join(botDir, "requirements.txt");
    if (fs.existsSync(reqs)) {
      spawnSync(python, ["-m", "pip", "install", "--user", "-r", reqs],
        { stdio: "inherit" });
    }
    const backendEnv = {
      ...process.env,
      PYTHONPATH: botDir,
      DEVBOX_ORIGIN: process.env.DEVBOX_ORIGIN ?? "https://app.devinai.net",
    };
    console.log(`Starting devbox_bot.desktop from ${botDir} on :${port}`);
    const child = spawn(python,
      ["-m", "devbox_bot.desktop", "--port", String(port)], {
        detached: true, stdio: "inherit", env: backendEnv,
      });
    child.unref();
    await waitForHealth(url);
    env.SAND_BACKEND_URL = url;
    env.CURSOR_API_BASE_URL = url;
    env.SAND_CURSOR_WEBSITE_URL = url;
  }
  if (options.printEnvOnly) {
    for (const name of Object.keys(env)) console.log(`${name}=<redacted>`);
    return;
  }
  console.log(`Launching ${appExe} against local DevBox backend ${url}`);
  const child = spawn(appExe, [], {
    detached: true,
    stdio: "inherit",
    env: { ...process.env, ...env },
  });
  child.unref();
}

const isMain = process.argv[1] != null
  && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  await main();
}
