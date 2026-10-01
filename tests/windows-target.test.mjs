import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function importConfig(env = {}) {
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", `
    import * as c from "${pathToFileURL(path.join(repoRoot, "scripts/lib/config.mjs")).href}";
    console.log(JSON.stringify({
      target: c.target,
      cachedRuntimeApp: c.cachedRuntimeApp,
      sourceAppDir: c.sourceAppDir,
      upstreamAsarSha256: c.upstreamAsarSha256,
      windowsOutputApp: c.windowsOutputApp,
      resources: c.runtimeResourcesDir("/x/app"),
    }));
  `], { env: { ...process.env, ...env }, encoding: "utf8" });
  return result;
}

test("default target is darwin-arm64 with unchanged literals", () => {
  const result = importConfig({ GROK_BOT_TARGET: "" });
  assert.equal(result.status, 0, result.stderr);
  const config = JSON.parse(result.stdout);
  assert.equal(config.target, "darwin-arm64");
  assert.ok(config.cachedRuntimeApp.endsWith(path.join("runtime", "Grok Bot.app")));
  assert.ok(config.sourceAppDir.endsWith(path.join("src", "app")));
  assert.equal(config.upstreamAsarSha256, "6665408168466f9cacc6087e917890c17f59d2e2e9c2404a5c4a59ad79c1de58");
  assert.ok(config.resources.endsWith(path.join("Contents", "Resources")));
});

test("win32-x64 target switches paths and asar sha", () => {
  const result = importConfig({ GROK_BOT_TARGET: "win32-x64" });
  assert.equal(result.status, 0, result.stderr);
  const config = JSON.parse(result.stdout);
  assert.equal(config.target, "win32-x64");
  assert.ok(config.cachedRuntimeApp.endsWith(path.join("runtime", "win32-x64", "app")));
  assert.ok(config.sourceAppDir.endsWith(path.join(".cache", "source-payloads", "win32-x64", "app")));
  assert.equal(config.upstreamAsarSha256, "38e85c0e5042c0257db7925e1e55709d6d155d90d92fe26ad654127d509766e0");
  assert.ok(config.windowsOutputApp.endsWith("Grok Bot 0.18 Reconstructed-win32-x64"));
  assert.ok(config.resources.endsWith("resources"));
});

test("invalid target throws", () => {
  const result = importConfig({ GROK_BOT_TARGET: "linux-x64" });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Unsupported GROK_BOT_TARGET/);
});

test("with-target.mjs sets GROK_BOT_TARGET for the child script", () => {
  const helper = path.join(repoRoot, ".cache", "with-target-check.mjs");
  mkdirSync(path.dirname(helper), { recursive: true });
  writeFileSync(helper, "console.log(process.env.GROK_BOT_TARGET);\n");
  const result = spawnSync(process.execPath, [
    path.join(repoRoot, "scripts/with-target.mjs"),
    "win32-x64", helper,
  ], { encoding: "utf8", env: { ...process.env, GROK_BOT_TARGET: "" } });
  rmSync(helper, { force: true });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), "win32-x64");
});

test("devbox-connect capability extraction", async () => {
  const { capabilityFromUrl, capabilityFromResponse, mintPreviewLink } = await import("../scripts/devbox-connect.mjs");
  const parsed = capabilityFromUrl("https://s-abc-1340.relay.example/health?tkn=v1.token&x=1#f");
  assert.equal(parsed.capability, "v1.token");
  assert.equal(parsed.baseUrl, "https://s-abc-1340.relay.example/health");
  assert.equal(capabilityFromUrl("https://h/").capability, null);
  // Real DevBox shape: token is a body field, the URL carries no tkn.
  const real = capabilityFromResponse({
    url: "https://s-abc-1341.relay.example/",
    session_path: "/api/...",
    token: "v1.bodycap",
  });
  assert.equal(real.capability, "v1.bodycap");
  assert.equal(real.baseUrl, "https://s-abc-1341.relay.example");

  const calls = [];
  const fakeFetch = async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      status: 200,
      json: async () => ({
        url: "https://s-abc-1341.relay.example/",
        session_path: "/api/...",
        token: "v1.cap",
      }),
    };
  };
  const minted = await mintPreviewLink({
    origin: "https://app.example",
    sessionId: "devin-1",
    apiKey: "key",
    localPort: 1341,
    fetchImpl: fakeFetch,
  });
  assert.equal(minted.capability, "v1.cap");
  assert.equal(minted.baseUrl, "https://s-abc-1341.relay.example");
  assert.equal(calls[0].url, "https://app.example/api/preview-link/devin-1?local_port=1341");
  assert.equal(calls[0].options.method, "PUT");
  assert.equal(calls[0].options.headers.Authorization, "Bearer key");
});
