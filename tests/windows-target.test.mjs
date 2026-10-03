import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
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

test("archive entry listing normalizes win32-style separator entries", async () => {
  const { archiveFileEntries, normalizeArchiveRelative } = await import("../scripts/lib/asar-integrity.mjs");
  assert.equal(normalizeArchiveRelative("\\dist\\deps\\x.js"), "dist/deps/x.js");
  assert.equal(normalizeArchiveRelative("/dist/deps/x.js"), "dist/deps/x.js");
  assert.equal(normalizeArchiveRelative("dist/deps/x.js"), "dist/deps/x.js");

  const statCalls = [];
  const entries = await archiveFileEntries("fake.asar", {
    listPackageImpl: () => [
      "\\dist\\deps\\x.js",
      "\\package.json",
      "\\dist\\deps", // directory entry: statFile returns no size
    ],
    statFileImpl: (_archive, p) => {
      statCalls.push(p);
      if (p.endsWith("deps")) return { files: {} };
      return { size: 3, integrity: { hash: "h" } };
    },
  });
  assert.deepEqual([...entries.keys()].sort(), ["dist/deps/x.js", "package.json"]);
  assert.ok(entries.get("dist/deps/x.js").size === 3);
  // statFile must receive a path.sep-joined path for its win32 traversal.
  assert.ok(statCalls.every(p => p === p.split("/").join(path.sep)));
});

test("gateway smoke redacts tokens and evaluates 401/200 matrix", async () => {
  const { runSmoke } = await import("../scripts/ci/devbox-gateway-smoke.mjs");
  const SECRETS = ["v1.cap1340", "v1.cap1341", "gw-secret-token"];
  const fakeFetch = async (url, options = {}) => {
    const headers = options.headers ?? {};
    const json = (status, body, contentType = "application/json") => ({
      ok: status >= 200 && status < 300,
      status,
      headers: new Map([["content-type", contentType]]),
      json: async () => body,
      arrayBuffer: async () => new ArrayBuffer(0),
    });
    if (url.includes("/api/preview-link/")) {
      const port = url.includes("1341") ? 1341 : 1340;
      return json(200, {
        url: `https://s-1-${port}.relay.example/`,
        session_path: "/api/x",
        token: `v1.cap${port}`,
      });
    }
    if (url.endsWith("/descriptor")) {
      return headers["x-anyrun-network-token"] === "v1.cap1341"
        ? json(200, { schema: 1, gatewayPort: 1340, gatewayToken: "gw-secret-token", hostVersion: "0.18.0" })
        : json(401, { error: "unauthorized" });
    }
    if (url.endsWith("/health")) {
      return headers["x-anyrun-network-token"] === "v1.cap1340"
        ? json(200, { ok: true }) : json(401, {});
    }
    if (url.endsWith("/api/listAgents")) {
      if (headers["x-anyrun-network-token"] !== "v1.cap1340") return json(401, {});
      return headers.Authorization === "Bearer gw-secret-token"
        ? json(200, { agents: [{}, {}] }) : json(401, {});
    }
    if (url.endsWith("/events")) {
      const stream = new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode("retry: 1000\n\ndata: {\"x\":1}\n\n"));
        },
      });
      return {
        ok: true, status: 200,
        headers: new Map([["content-type", "text/event-stream"]]),
        body: stream,
        json: async () => ({}),
        arrayBuffer: async () => new ArrayBuffer(0),
      };
    }
    return json(404, { error: "nope" });
  };
  const { result, secretsToMask } = await runSmoke({
    sessionId: "devin-1", apiKey: "k", fetchImpl: fakeFetch,
    envFilePath: null,
  });
  assert.equal(result.ok, true, JSON.stringify(result.steps));
  const serialized = JSON.stringify(result);
  for (const secret of SECRETS) {
    assert.ok(!serialized.includes(secret), `secret leaked: ${secret}`);
  }
  assert.ok(secretsToMask.includes("gw-secret-token"));
  const byName = Object.fromEntries(result.steps.map(s => [s.name, s]));
  assert.equal(byName["listAgents-no-bearer-401"].status, "pass");
  assert.equal(byName["listAgents-with-bearer-200"].status, "pass");
  assert.equal(byName["listAgents-with-bearer-200"].agentCount, 2);
  assert.equal(byName["listAgents-bogus-capability-401"].status, "pass");
  assert.equal(byName["events-sse"].firstDataLine, '{"x":1}');
  assert.equal(byName.descriptor.gatewayPort, 1340);
  // gatewayToken must not appear in the recorded descriptor keys.
  assert.ok(!byName.descriptor.keys.includes("gatewayToken"));
});

test("integrity check still reports missing-archive-entry on real drift", async () => {
  const { verifyStagedPackageIntegrity } = await import("../scripts/lib/asar-integrity.mjs");
  const stageRoot = mkdtempSync(path.join(tmpdir(), "grok-bot-stage-"));
  try {
    writeFileSync(path.join(stageRoot, "a.txt"), "payload");
    const sha256 = b => createHash("sha256").update(b).digest("hex");
    const before = new Map([["a.txt", { bytes: 7, sha256: sha256("payload") }]]);
    // Archive listing that lacks the staged file -> missing-archive-entry.
    await assert.rejects(
      verifyStagedPackageIntegrity({
        stageRoot,
        archivePath: "fake.asar",
        unpackedRoot: "fake.unpacked",
        before,
        archiveEntriesImpl: async () => new Map(),
      }),
      /"relative":"a\.txt","kind":"missing-archive-entry"/,
    );
  } finally {
    rmSync(stageRoot, { recursive: true, force: true });
  }
});
