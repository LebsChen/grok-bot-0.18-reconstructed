// Live DevBox gateway smoke for the Windows package: mint preview-link
// capabilities, fetch the descriptor, and exercise the guest gateway
// through the relay (health, listAgents auth matrix, SSE /events).
// Never writes tokens, capabilities, or Authorization values to the
// output JSON or stdout.
import { appendFile, writeFile } from "node:fs/promises";
import os from "node:os";
import { fetchDescriptor, mintPreviewLink } from "../devbox-connect.mjs";

const SENSITIVE = new Set(["gatewayToken", "token"]);

async function timed(steps, name, fn) {
  const started = performance.now();
  const step = { name, ms: 0 };
  steps.push(step);
  try {
    const extra = await fn();
    step.status = "pass";
    Object.assign(step, extra ?? {});
  } catch (error) {
    step.status = "fail";
    step.error = String(error?.message ?? error);
  }
  step.ms = Math.round(performance.now() - started);
  return step;
}

async function firstSseDataLine(response, deadlineMs = 15_000) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  const deadline = Date.now() + deadlineMs;
  try {
    while (Date.now() < deadline) {
      const { value, done } = await reader.read();
      if (done) break;
      buffered += decoder.decode(value, { stream: true });
      const match = buffered.match(/(^|\n)data:\s*(.*)/);
      if (match) return match[2].slice(0, 200);
    }
    return null;
  } finally {
    await reader.cancel().catch(() => {});
  }
}

export async function runSmoke({
  origin = process.env.DEVBOX_ORIGIN ?? "https://app.devinai.net",
  sessionId = process.env.DEVBOX_SESSION_ID,
  apiKey = process.env.DEVBOX_API_KEY,
  fetchImpl = fetch,
  envFilePath = process.env.GITHUB_ENV,
} = {}) {
  if (!sessionId) throw new Error("DEVBOX_SESSION_ID is required");
  if (!apiKey) throw new Error("DEVBOX_API_KEY is required");
  const steps = [];
  const state = {};
  const secretsToMask = [];

  const expect = (condition, message) => {
    if (!condition) throw new Error(message);
  };

  await timed(steps, "mint-preview-link-1340", async () => {
    state.gateway = await mintPreviewLink({
      origin, sessionId, apiKey, localPort: 1340, fetchImpl });
    secretsToMask.push(state.gateway.capability);
    expect(state.gateway.capability?.startsWith("v1."), "no v1 capability for 1340");
    expect(state.gateway.baseUrl.startsWith("https://"), "no https base for 1340");
    return { baseHost: new URL(state.gateway.baseUrl).host };
  });
  await timed(steps, "mint-preview-link-1341", async () => {
    state.descriptor = await mintPreviewLink({
      origin, sessionId, apiKey, localPort: 1341, fetchImpl });
    secretsToMask.push(state.descriptor.capability);
    expect(state.descriptor.capability?.startsWith("v1."), "no v1 capability for 1341");
    return { baseHost: new URL(state.descriptor.baseUrl).host };
  });

  await timed(steps, "descriptor", async () => {
    const payload = await fetchDescriptor({
      descriptorBase: state.descriptor.baseUrl,
      capability: state.descriptor.capability,
      fetchImpl,
    });
    state.gatewayToken = payload.gatewayToken;
    secretsToMask.push(state.gatewayToken);
    expect(typeof state.gatewayToken === "string" && state.gatewayToken.length > 0,
      "descriptor had no gatewayToken");
    return {
      keys: Object.keys(payload).filter(k => !SENSITIVE.has(k)),
      gatewayPort: payload.gatewayPort,
      hostVersion: payload.hostVersion,
    };
  });

  const authed = (path, { bearer, capability, headers = {} } = {}) =>
    fetchImpl(`${state.gateway.baseUrl}${path}`, {
      method: path === "/api/listAgents" ? "POST" : "GET",
      headers: {
        ...(capability ? { "x-anyrun-network-token": capability } : {}),
        ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
        ...(path === "/api/listAgents" ? { "content-type": "application/json" } : {}),
        ...headers,
      },
      ...(path === "/api/listAgents" ? { body: "{}" } : {}),
    });

  await timed(steps, "health", async () => {
    const res = await authed("/health", { capability: state.gateway.capability });
    expect(res.status === 200, `/health expected 200, got ${res.status}`);
    const body = await res.json();
    return { ok: body.ok === true };
  });
  await timed(steps, "listAgents-no-bearer-401", async () => {
    const res = await authed("/api/listAgents", { capability: state.gateway.capability });
    expect(res.status === 401, `expected 401 without bearer, got ${res.status}`);
    await res.arrayBuffer();
    return { httpStatus: res.status };
  });
  await timed(steps, "listAgents-with-bearer-200", async () => {
    const res = await authed("/api/listAgents", {
      capability: state.gateway.capability, bearer: state.gatewayToken });
    expect(res.status === 200, `expected 200 with bearer, got ${res.status}`);
    const body = await res.json();
    const agents = Array.isArray(body) ? body : body?.agents;
    return { httpStatus: res.status, agentCount: Array.isArray(agents) ? agents.length : null };
  });
  await timed(steps, "listAgents-bogus-capability-401", async () => {
    const res = await authed("/api/listAgents", {
      capability: "v1.invalid", bearer: state.gatewayToken });
    expect(res.status === 401, `expected 401 for bogus capability, got ${res.status}`);
    await res.arrayBuffer();
    return { httpStatus: res.status };
  });
  await timed(steps, "events-sse", async () => {
    const started = performance.now();
    const res = await authed("/events", {
      capability: state.gateway.capability,
      bearer: state.gatewayToken,
      headers: { Accept: "text/event-stream" },
    });
    expect(res.status === 200, `/events expected 200, got ${res.status}`);
    const contentType = res.headers.get("content-type") ?? "";
    expect(contentType.includes("text/event-stream"),
      `/events content-type ${contentType} is not SSE`);
    const firstData = await firstSseDataLine(res);
    return {
      contentType,
      timeToFirstByteMs: Math.round(performance.now() - started),
      firstDataLine: firstData,
    };
  });

  const result = {
    platform: os.platform(),
    arch: os.arch(),
    node: process.version,
    sessionId,
    steps,
    ok: steps.every(step => step.status === "pass"),
  };
  const env = {
    SAND_HOST_GATEWAY_URL: (state.gateway?.baseUrl ?? "").replace(/\/$/, ""),
    SAND_HOST_GATEWAY_TOKEN: state.gatewayToken ?? "",
    SAND_HOST_GATEWAY_NETWORK_TOKEN: state.gateway?.capability ?? "",
  };
  return { result, secretsToMask, env };
}

async function main() {
  const outPath = process.argv[2] ?? "gateway-smoke.json";
  const { result, secretsToMask, env } = await runSmoke({});
  // Write the JSON first so a failing expectation still leaves evidence.
  await writeFile(outPath, `${JSON.stringify(result, null, 2)}\n`);
  const envFile = process.env.GITHUB_ENV;
  if (envFile) {
    for (const value of secretsToMask) {
      if (value) console.log(`::add-mask::${value}`);
    }
    const lines = Object.entries(env)
      .filter(([, value]) => value)
      .map(([name, value]) => `${name}=${value}`)
      .join("\n");
    if (lines) await appendFile(envFile, `${lines}\n`);
  }
  process.exit(result.ok ? 0 : 1);
}

if (process.argv[1] != null
    && new URL(import.meta.url).pathname === new URL(`file://${process.argv[1]}`).pathname) {
  main().catch(error => {
    console.error(error?.message ?? error);
    process.exit(1);
  });
}
