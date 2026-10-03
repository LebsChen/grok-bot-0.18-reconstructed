// Live DevBox-backend smoke: real binary Connect against https://app.devinai.net
// Steps: headless login approve -> poll -> refresh -> GetMe -> AvailableModels
// -> EnsureSandBox -> gateway health/listAgents/events -> guest inference
// -> direct InferenceService.Stream. Writes redacted JSON (no tokens).
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";

import {
  collectAssistantText,
  connectDevboxSession,
  createDevboxGateway,
  ensureDevboxSandbox,
  gatewayRequest,
} from "./devbox-backend-smoke-helpers.mjs";

const ORIGIN = process.env.DEVBOX_ORIGIN ?? "https://app.devinai.net";
const API_KEY = process.env.DEVBOX_API_KEY ?? "";
const OUT = process.argv[2] ?? "devbox-backend-smoke.json";

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

function scrubDiagnosticText(value) {
  let text = String(value ?? "");
  for (const secret of [
    API_KEY,
    process.env.GROKBOT_LLM_API_KEY ?? "",
    process.env.github_pat ?? "",
  ].filter(Boolean)) {
    text = text.replaceAll(secret, "<redacted>");
  }
  return text
    .replace(/\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer <redacted>")
    .replace(/\b(?:eyJhbGci|gbr_local_|cog_|ghp_|github_pat_|sk-)[A-Za-z0-9._-]{8,}\b/g,
             "<redacted>")
    .slice(0, 120);
}

function findQuotaError(value) {
  if (typeof value === "string") {
    if (/API_KEY_QUOTA_EXHAUSTED/i.test(value)) {
      return "API_KEY_QUOTA_EXHAUSTED";
    }
    return /\bquota\b/i.test(value) ? "quota" : "";
  }
  if (Array.isArray(value)) {
    return value.map(findQuotaError).find(Boolean) ?? "";
  }
  if (!value || typeof value !== "object") return "";
  return Object.values(value).map(findQuotaError).find(Boolean) ?? "";
}

function findQuotaErrorText(value) {
  if (typeof value === "string") {
    return /API_KEY_QUOTA_EXHAUSTED|\bquota\b/i.test(value)
      ? scrubDiagnosticText(value) : "";
  }
  if (Array.isArray(value)) {
    return value.map(findQuotaErrorText).find(Boolean) ?? "";
  }
  if (!value || typeof value !== "object") return "";
  return Object.values(value).map(findQuotaErrorText).find(Boolean) ?? "";
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

const session = await connectDevboxSession({ origin: ORIGIN, apiKey: API_KEY, step });
if (!session) {
  writeFileSync(OUT, JSON.stringify({ ok: false, steps }, null, 1));
  process.exit(1);
}
const { dash, grokBot, ai, inference } = session;

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
const box = await ensureDevboxSandbox(grokBot, step);

let gateway = null;
if (box) {
  gateway = createDevboxGateway(box);
  const gh = async (method, path, { bearer } = {}) =>
    gatewayRequest(gateway, method, path, { bearer });
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
    let assistantMessagePresent = false;
    let transcriptRetrieved = false;
    let quotaErrorCode = "";
    let transcriptDiagnostic = "";
    for (let i = 0; i < 30; i += 1) {
      await new Promise(r => setTimeout(r, 2000));
      const tr = await fetch(
        `${gateway.base}/api/getAgentTranscript`,
        { method: "POST",
          headers: { "x-anyrun-network-token": gateway.networkToken,
                     authorization: `Bearer ${gateway.token}`,
                     "content-type": "application/json" },
          body: JSON.stringify({ id: agentId }) });
      if (!tr.ok) {
        const errorBody = await tr.json().catch(() => null);
        const errorText = scrubDiagnosticText(errorBody?.error);
        throw new Error(
          `getAgentTranscript returned HTTP ${tr.status}; error=${errorText}; ` +
          "transcriptRetrieved=false; assistantMessagePresent=false; " +
          "assistantReplyFound=false; quotaErrorInTranscript=false; " +
          "execInvoked=unknown");
      }
      transcriptRetrieved = true;
      const body = await tr.json().catch(() => null);
      const assistantTexts = collectAssistantText(body);
      assistantMessagePresent ||= assistantTexts.length > 0;
      assistantReply = assistantTexts.find(text => /\bok\b/i.test(text)) ?? "";
      quotaErrorCode ||= findQuotaError(body);
      transcriptDiagnostic ||= findQuotaErrorText(body);
      execInvoked ||= hasExecToolCall(body);
      if (assistantReply || quotaErrorCode) break;
    }
    if (!assistantReply) {
      throw new Error(
        `assistant reply not found; transcriptRetrieved=${transcriptRetrieved}; ` +
        `assistantMessagePresent=${assistantMessagePresent}; ` +
        `assistantReplyFound=false; quotaErrorInTranscript=${Boolean(quotaErrorCode)}; ` +
        `quotaErrorCode=${quotaErrorCode || "none"}; ` +
        `transcriptDiagnostic=${transcriptDiagnostic || "none"}; ` +
        `execInvoked=${execInvoked}`);
    }
    return { createStatus: create.status, sendStatus: send.status,
             agentId: agentId ? "<set>" : "",
             transcriptRetrieved,
             assistantMessagePresent,
             assistantReplyFound: Boolean(assistantReply),
             assistantReplySnippet: assistantReply.slice(0, 80),
             quotaErrorInTranscript: Boolean(quotaErrorCode),
             quotaErrorCode,
             transcriptDiagnostic,
             execInvoked };
  }, (r) => {
    if (r?.createStatus !== 200 || r.sendStatus < 200 || r.sendStatus >= 300) {
      throw new Error(
        `agent request failed (create=${r?.createStatus} send=${r?.sendStatus})`);
    }
    if (!r.assistantReplyFound) {
      throw new Error(
        `assistant transcript did not contain the expected reply; ` +
        `transcriptRetrieved=${r.transcriptRetrieved}; ` +
        `assistantMessagePresent=${r.assistantMessagePresent}; ` +
        `quotaErrorInTranscript=${r.quotaErrorInTranscript}; ` +
        `quotaErrorCode=${r.quotaErrorCode || "none"}; ` +
        `transcriptDiagnostic=${r.transcriptDiagnostic || "none"}; ` +
        `execInvoked=${r.execInvoked}`);
    }
    if (r.execInvoked) {
      throw new Error(
        `agent turn invoked the exec tool; transcriptRetrieved=${r.transcriptRetrieved}; ` +
        `assistantReplyFound=${r.assistantReplyFound}; execInvoked=true`);
    }
    return { createStatus: r.createStatus, sendStatus: r.sendStatus,
             transcriptRetrieved: r.transcriptRetrieved,
             assistantMessagePresent: r.assistantMessagePresent,
             assistantReplyFound: r.assistantReplyFound,
             quotaErrorInTranscript: r.quotaErrorInTranscript,
             quotaErrorCode: r.quotaErrorCode,
             transcriptDiagnostic: r.transcriptDiagnostic,
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
