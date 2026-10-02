// Live DevBox-backend smoke: real binary Connect against https://app.devinai.net
// Steps: headless login approve -> poll -> refresh -> GetMe -> AvailableModels
// -> EnsureSandBox -> gateway health/listAgents/events -> guest inference
// -> direct InferenceService.Stream. Writes redacted JSON (no tokens).
import { createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-node";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";

import { DashboardService } from "../../source/packages/proto/generated/aiserver/v1/dashboard_connect.js";
import { GrokBotService } from "../../source/packages/proto/generated/aiserver/v1/grok_bot_connect.js";
import { AiService } from "../../source/packages/proto/generated/aiserver/v1/aiserver_connect.js";
import { InferenceService } from "../../source/packages/proto/generated/aiserver/v1/inference_connect.js";

const ORIGIN = process.env.DEVBOX_ORIGIN ?? "https://app.devinai.net";
const API_KEY = process.env.DEVBOX_API_KEY ?? "";
const OUT = process.argv[2] ?? "devbox-backend-smoke.json";
const PROD_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB";

const steps = [];
function record(name, status, ms, extra = {}) {
  steps.push({ name, status, ms, ...extra });
  console.log(`${status === "pass" ? "PASS" : "FAIL"} ${name} (${ms}ms)`);
}
const t0 = Date.now();
async function step(name, fn, extraFn) {
  const start = Date.now();
  try {
    const out = await fn();
    record(name, "pass", Date.now() - start,
           extraFn ? extraFn(out) : {});
    return out;
  } catch (error) {
    record(name, "fail", Date.now() - start,
           { error: String(error?.message ?? error).slice(0, 300) });
    return undefined;
  }
}

const b64url = (buf) => Buffer.from(buf).toString("base64url");
const uuid = randomUUID();
const verifier = b64url(randomBytes(32));
const challenge = b64url(createHash("sha256").update(verifier).digest());

async function http(method, path, { token, json, headers } = {}) {
  const resp = await fetch(`${ORIGIN}${path}`, {
    method,
    headers: {
      ...(json ? { "content-type": "application/json" } : {}),
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
    body: json ? JSON.stringify(json) : undefined,
  });
  const text = await resp.text();
  let body;
  try { body = JSON.parse(text); } catch { body = text.slice(0, 200); }
  return { status: resp.status, body };
}

function collectAssistantText(value, result = []) {
  if (Array.isArray(value)) {
    for (const item of value) collectAssistantText(item, result);
    return result;
  }
  if (!value || typeof value !== "object") return result;
  const role = String(value.role ?? value.speaker ?? value.author ?? "")
    .toLowerCase();
  if (role === "assistant") {
    if (typeof value.text === "string") result.push(value.text);
    if (typeof value.content === "string") result.push(value.content);
    if (Array.isArray(value.content)) {
      for (const part of value.content) {
        if (typeof part?.text === "string") result.push(part.text);
      }
    } else if (typeof value.content?.text === "string") {
      result.push(value.content.text);
    }
  }
  for (const child of Object.values(value)) collectAssistantText(child, result);
  return result;
}

function hasExecToolCall(value) {
  if (Array.isArray(value)) return value.some(hasExecToolCall);
  if (!value || typeof value !== "object") return false;
  const isExec = (name) =>
    typeof name === "string" &&
    name.toLowerCase().replace(/[_-]/g, "") === "exec";
  const names = [
    value.toolName,
    value.tool_name,
    value.name,
    value.function?.name,
    value.tool?.name,
    value.tool?.case,
    value.value?.tool?.case,
  ];
  if (names.some(isExec)) return true;
  if (isExec(value.toolResult?.toolName ?? value.toolResult?.name)) return true;
  return Object.values(value).some(hasExecToolCall);
}

// 1. headless approve
const approve = await step("loginDeepControl/approve", () =>
  http("POST", "/loginDeepControl/approve", {
    token: API_KEY, json: { uuid, challenge } }), (r) => ({ httpStatus: r.status }));
if (approve?.status !== 200) {
  writeFileSync(OUT, JSON.stringify({ ok: false, steps }, null, 1));
  process.exit(1);
}

// 2. poll -> tokens
const poll = await step("auth/poll", () =>
  http("GET", `/auth/poll?uuid=${uuid}&verifier=${verifier}`),
  (r) => ({ httpStatus: r.status, keys: Object.keys(r.body ?? {}) }));
const accessToken = poll?.body?.accessToken;
const refreshToken = poll?.body?.refreshToken;
if (!accessToken || !refreshToken) {
  writeFileSync(OUT, JSON.stringify({ ok: false, steps }, null, 1));
  process.exit(1);
}

// 3. refresh
const refreshed = await step("oauth/token refresh", () =>
  http("POST", "/oauth/token", {
    json: { client_id: PROD_CLIENT_ID, grant_type: "refresh_token",
            refresh_token: refreshToken } }),
  (r) => {
    if (r.status !== 200 || !r.body?.access_token) {
      throw new Error(`refresh failed (HTTP ${r.status})`);
    }
    return { httpStatus: r.status, keys: Object.keys(r.body ?? {}) };
  });
const liveAccess = refreshed?.body?.access_token ?? accessToken;

// Connect transport with bearer
const transport = createConnectTransport({
  baseUrl: ORIGIN,
  httpVersion: "1.1",
  interceptors: [(next) => async (req) => {
    req.header.set("authorization", `Bearer ${liveAccess}`);
    return next(req);
  }],
});

const dash = createClient(DashboardService, transport);
const grokBot = createClient(GrokBotService, transport);
const ai = createClient(AiService, transport);
const inference = createClient(InferenceService, transport);

// 4. GetMe
await step("DashboardService.GetMe", () => dash.getMe({}),
  (r) => ({ authId: r.authId ? "<set>" : "", hasEmail: Boolean(r.email) }));

// 5. AvailableModels
const catalog = await step("AiService.AvailableModels", () =>
  ai.availableModels({ useModelParameters: true, scope: 1 }),
  (r) => {
    const names = r.models.map(m => m.name);
    if (!names.length) throw new Error("model catalog is empty");
    if (process.env.GROKBOT_LLM_MODEL &&
        !names.includes(process.env.GROKBOT_LLM_MODEL)) {
      throw new Error("configured LLM model is not available");
    }
    return { modelCount: names.length, names: names.slice(0, 5) };
  });

// 6. EnsureSandBox
const box = await step("GrokBotService.EnsureSandBox", () =>
  grokBot.ensureSandBox({}),
  (r) => {
    if (!r.podId || !r.gatewayUrl || !r.gatewayToken || !r.networkToken) {
      throw new Error("EnsureSandBox returned incomplete session details");
    }
    return { podId: r.podId, cluster: r.cluster,
             hasGatewayUrl: true, hasGatewayToken: true,
             hasNetworkToken: true };
  });

let gateway = null;
if (box) {
  gateway = {
    base: box.gatewayUrl, token: box.gatewayToken,
    networkToken: box.networkToken,
  };
  const gh = async (method, path, { bearer } = {}) =>
    fetch(`${gateway.base}${path}`, {
      method,
      headers: {
        "x-anyrun-network-token": gateway.networkToken,
        ...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
        ...(method === "POST"
            ? { "content-type": "application/json" } : {}),
      },
      body: method === "POST" ? "{}" : undefined,
    });
  // 7. gateway /health
  await step("gateway /health", async () => {
    const resp = await gh("GET", "/health");
    return { status: resp.status };
  }, (r) => {
    if (r.status !== 200) throw new Error(`/health returned HTTP ${r.status}`);
    return { httpStatus: r.status };
  });
  // 8. listAgents unauth -> 401
  await step("gateway /api/listAgents no-bearer 401", async () => {
    const resp = await gh("POST", "/api/listAgents");
    return { status: resp.status };
  }, (r) => {
    if (r.status !== 401) {
      throw new Error(`unauthenticated listAgents returned HTTP ${r.status}`);
    }
    return { httpStatus: r.status };
  });
  // 9. listAgents with gateway token -> 200
  const agents = await step("gateway /api/listAgents bearer", async () => {
    const resp = await gh("POST", "/api/listAgents",
                          { bearer: gateway.token });
    const body = await resp.json().catch(() => null);
    return { status: resp.status, count: Array.isArray(body) ? body.length : -1 };
  }, (r) => {
    if (r.status !== 200 || r.count < 0) {
      throw new Error(`authenticated listAgents returned HTTP ${r.status}`);
    }
    return { httpStatus: r.status, agentCount: r.count };
  });
  // 10. /events first SSE frame
  await step("gateway /events first frame", async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const resp = await fetch(`${gateway.base}/events`, {
        headers: {
          "x-anyrun-network-token": gateway.networkToken,
          authorization: `Bearer ${gateway.token}`,
          accept: "text/event-stream",
        },
        signal: controller.signal,
      });
      const reader = resp.body.getReader();
      const started = Date.now();
      let chunk = "";
      const decoder = new TextDecoder();
      while (Date.now() - started < 15000) {
        const { done, value } = await reader.read();
        if (done) break;
        chunk += decoder.decode(value, { stream: true });
        if (chunk.includes("data:")) break;
      }
      controller.abort();
      const hasDataLine = (chunk.match(/data:[^\n]*/) ?? [""])[0]
        .startsWith("data:");
      return { status: resp.status, contentType: resp.headers.get("content-type"),
               hasDataLine };
    } finally { clearTimeout(timer); }
  }, (r) => {
    if (r.status !== 200 || !r.contentType?.includes("text/event-stream") ||
        !r.hasDataLine) {
      throw new Error("gateway events stream did not return its first frame");
    }
    return { httpStatus: r.status, contentType: r.contentType,
             hasDataLine: true };
  });

  // 11. guest-originated inference: /api/sendPrompt through the gateway so
  // host-main calls /sand-box/inference-credential + InferenceService.Stream
  await step("guest inference via /api/sendPrompt", async () => {
    const create = await fetch(`${gateway.base}/api/createAgent`, {
      method: "POST",
      headers: { "x-anyrun-network-token": gateway.networkToken,
                 authorization: `Bearer ${gateway.token}`,
                 "content-type": "application/json" },
      body: JSON.stringify({ name: "smoke", description: "",
        origin: "api", clientNonce: randomUUID() }),
    });
    if (!create.ok) return { createStatus: create.status };
    const created = await create.json();
    const agentId = created?.agent?.id ?? created?.id
      ?? created?.agentId ?? created?.agent_id;
    const send = await fetch(`${gateway.base}/api/sendPrompt`, {
      method: "POST",
      headers: { "x-anyrun-network-token": gateway.networkToken,
                 authorization: `Bearer ${gateway.token}`,
                 "content-type": "application/json" },
      body: JSON.stringify({ agentId, prompt: "Reply with the single word: ok" }),
    });
    let assistantReply = "";
    let execInvoked = false;
    for (let i = 0; i < 30; i += 1) {
      await new Promise(r => setTimeout(r, 2000));
      const tr = await fetch(
        `${gateway.base}/api/getTranscript`,
        { method: "POST",
          headers: { "x-anyrun-network-token": gateway.networkToken,
                     authorization: `Bearer ${gateway.token}`,
                     "content-type": "application/json" },
          body: JSON.stringify({ agentId }) });
      if (!tr.ok) throw new Error(`getTranscript returned HTTP ${tr.status}`);
      const body = await tr.json().catch(() => null);
      const assistantTexts = collectAssistantText(body);
      assistantReply = assistantTexts.find(text => /\bok\b/i.test(text)) ?? "";
      execInvoked ||= hasExecToolCall(body);
      if (assistantReply) break;
    }
    return { createStatus: create.status, sendStatus: send.status,
             agentId: agentId ? "<set>" : "",
             assistantReplyFound: Boolean(assistantReply),
             assistantReplySnippet: assistantReply.slice(0, 80),
             execInvoked };
  }, (r) => {
    if (r?.createStatus !== 200 || r.sendStatus < 200 || r.sendStatus >= 300) {
      throw new Error(
        `agent request failed (create=${r?.createStatus} send=${r?.sendStatus})`);
    }
    if (!r.assistantReplyFound) {
      throw new Error("assistant transcript did not contain the expected reply");
    }
    if (r.execInvoked) throw new Error("agent turn invoked the exec tool");
    return { createStatus: r.createStatus, sendStatus: r.sendStatus,
             assistantReplyFound: r.assistantReplyFound,
             execInvoked: r.execInvoked };
  });
}

// 12. direct InferenceService.Stream
await step("InferenceService.Stream direct", async () => {
  const req = {
    modelId: process.env.GROKBOT_LLM_MODEL
      ?? catalog?.models?.[0]?.name ?? "",
    invocationId: randomUUID(),
    messages: [{ role: 1, content: { case: "text", value: "Say the word: ping" } }],
  };
  let textParts = 0, usageSeen = false, errMsg = "", frames = 0;
  try {
    for await (const m of inference.stream(req)) {
      frames += 1;
      const inner = m.response ?? m;
      if (inner.case === "textPart" || inner.textPart) textParts += 1;
      if (inner.case === "usage" || inner.usage) usageSeen = true;
      const err = inner.case === "error" ? inner.value
        : (inner.error ?? m.error);
      if (err) errMsg = (err.message ?? String(err))?.slice(0, 160) ?? "err";
    }
  } catch (error) {
    return { frames, error: String(error?.message ?? error).slice(0, 200) };
  }
  return { frames, textParts, usageSeen, errMsg };
}, (r) => {
  if (!(r?.textParts > 0)) throw new Error(
    `no text frames (frames=${r?.frames} err=${r?.errMsg})`);
  if (!r?.usageSeen) throw new Error("no usage frame");
  return { frames: r.frames, textParts: r.textParts,
           usageSeen: r.usageSeen };
});

// Box-side inference is still covered by sendPrompt; direct Stream is
// optional only when no LLM API key is configured for the desktop.
const streamStep = steps.find(s => s.name === "InferenceService.Stream direct");
if (streamStep?.status === "fail" &&
    !process.env.GROKBOT_LLM_API_KEY) {
  streamStep.status = "skip";
  streamStep.note = "GROKBOT_LLM_API_KEY absent; skipping";
}
const ok = steps.length === 12 &&
  steps.every(s => s.status === "pass" || s.status === "skip");
writeFileSync(OUT, JSON.stringify({
  ok, origin: ORIGIN, node: process.version,
  platform: process.platform, arch: process.arch,
  durationMs: Date.now() - t0, steps,
}, null, 1));
console.log(`\nwrote ${OUT}: ok=${ok}`);
process.exit(ok ? 0 : 1);
