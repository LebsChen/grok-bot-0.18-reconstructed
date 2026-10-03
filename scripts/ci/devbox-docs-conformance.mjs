import { randomUUID } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import {
  collectAssistantText,
  connectDevboxSession,
  createDevboxGateway,
  ensureDevboxSandbox,
  gatewayRequest,
} from "./devbox-backend-smoke-helpers.mjs";

const ORIGIN = process.env.DEVBOX_ORIGIN ?? "https://app.devinai.net";
const API_KEY = process.env.DEVBOX_API_KEY ?? "";
const OUT = resolve(process.argv.includes("--out")
  ? process.argv[process.argv.indexOf("--out") + 1]
  : process.argv[2] ?? "devbox-docs-conformance");
const EXPECTED_BOX_PREFIX = "a8dce654";
const RUN_SUFFIX = `${Date.now().toString(36)}-${randomUUID().slice(0, 8)}`;
const TIMEOUT_MS = 180_000;
const POLL_MS = 2_000;

const matrixRows = {
  "named-bot": "Create a Bot with a name, role, description, and first task ([Get started])",
  "parallel-bots": "Multiple Bots run in parallel, with individual screens and one computer-use task per screen ([Grok Bot])",
  "bot-to-bot": "Bots message each other, collaborate in groups, and hand off work ([Grok Bot], [Work])",
  group: "Group chats support 2–6 Bots and mentions ([Work])",
  memory: "Bot memory accumulates work/preferences while conversation context remains Bot-specific ([Grok Bot], [Work])",
  skill: "Save reusable skills and teach workflows by demonstration (up to 10 minutes, without microphone audio) ([Work])",
  "routine-run": "Test/manage routines; each Bot can have up to 50, with the latest 20 runs retained per routine ([Work])",
  "routine-unattended": "Create scheduled/event-triggered routines that run in the cloud ([Grok Bot], [Work])",
  attachment: "Attach files and links to a conversation ([Work])",
  search: "Search prior conversations and inspect completed work ([Work])",
  "secret-request": "Request secrets securely and keep values masked from logs/model context ([Work])",
  notifications: "Unread/needs-attention states, push notifications, and in-app errors ([Settings])",
  settings: "Account, appearance, timezone, schedule, and local-command preferences ([Settings])",
  "box-status": "Bots for one user use a shared persistent cloud computer with browser, files, and terminal ([Grok Bot], [Work])",
  teach: "Save reusable skills and teach workflows by demonstration (up to 10 minutes, without microphone audio) ([Work])",
  plugins: "Install/manage plugins, connectors, and skills packs ([Settings], [Work])",
  sharing: "Share Bots as public templates while protecting private content ([Work])",
  delete: "Delete a Bot and remove/retain associated state as documented ([Work])",
};

class BlockedError extends Error {
  constructor(message) {
    super(message);
    this.name = "BlockedError";
  }
}

function isObject(value) {
  return value != null && typeof value === "object";
}

function parseText(text) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function extractId(value) {
  if (!isObject(value)) return undefined;
  for (const candidate of [
    value.agent?.id,
    value.agent?.agentId,
    value.workflow?.id,
    value.automation?.id,
    value.id,
    value.agentId,
    value.agent_id,
    value.workflowId,
    value.automationId,
  ]) {
    if (typeof candidate === "string" && candidate.length > 0) return candidate;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const id = extractId(item);
      if (id) return id;
    }
  }
  for (const child of Object.values(value)) {
    const id = extractId(child);
    if (id) return id;
  }
  return undefined;
}

function listBody(value) {
  if (Array.isArray(value)) return value;
  if (!isObject(value)) return [];
  for (const key of ["agents", "workflows", "automations", "memories", "items", "results", "servers"]) {
    if (Array.isArray(value[key])) return value[key];
  }
  return [];
}

function findByName(value, name) {
  return listBody(value).find((item) =>
    item?.name === name || item?.profile?.name === name);
}

function hasMarker(value, marker) {
  return JSON.stringify(value ?? "").includes(marker);
}

function textIncludes(value, pattern) {
  return collectAssistantText(value).some((text) => pattern.test(text));
}

function timestampOf(value) {
  if (!isObject(value)) return undefined;
  if (typeof value.timestampMs === "number") return value.timestampMs;
  if (typeof value.createdAtMs === "number") return value.createdAtMs;
  if (typeof value.timestamp === "number") return value.timestamp;
  if (typeof value.timestamp === "string") {
    const parsed = Date.parse(value.timestamp);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function findToolNames(value, result = []) {
  if (Array.isArray(value)) {
    for (const item of value) findToolNames(item, result);
    return [...new Set(result)];
  }
  if (!isObject(value)) return [...new Set(result)];
  const isToolKind = /tool[-_ ]?(call|result)/i.test(
    String(value.kind ?? value.type ?? ""),
  );
  for (const key of ["toolName", "tool_name", "functionName", "function_name"]) {
    if (typeof value[key] === "string") result.push(value[key]);
  }
  if (isToolKind && typeof value.name === "string") result.push(value.name);
  for (const child of Object.values(value)) findToolNames(child, result);
  return [...new Set(result)];
}

function hasMarkerInToolRecord(value, marker) {
  if (Array.isArray(value)) return value.some((item) => hasMarkerInToolRecord(item, marker));
  if (!isObject(value)) return false;
  const isToolKind = /tool[-_ ]?(call|result)/i.test(
    String(value.kind ?? value.type ?? ""),
  );
  if (isToolKind && hasMarker(value, marker)) return true;
  return Object.values(value).some((child) => hasMarkerInToolRecord(child, marker));
}

function findSecretRequest(value) {
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findSecretRequest(item);
      if (found) return found;
    }
    return undefined;
  }
  if (!isObject(value)) return undefined;
  if (value.kind === "send-message" &&
      value.message?.type === "secret-request" &&
      typeof value.id === "string") return value;
  for (const child of Object.values(value)) {
    const found = findSecretRequest(child);
    if (found) return found;
  }
  return undefined;
}

function findAssistantMarkerTimestamp(value, marker, inheritedTimestamp) {
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findAssistantMarkerTimestamp(item, marker, inheritedTimestamp);
      if (found != null) return found;
    }
    return undefined;
  }
  if (!isObject(value)) return undefined;
  const currentTimestamp = timestampOf(value) ?? inheritedTimestamp;
  for (const child of Object.values(value)) {
    const found = findAssistantMarkerTimestamp(child, marker, currentTimestamp);
    if (found != null) return found;
  }
  if (collectAssistantText(value).some((text) => text.includes(marker))) return currentTimestamp;
  return undefined;
}

function sensitiveKey(key) {
  return /authorization|cookie|password|secret|token|credential|api[-_]?key|bytesbase64|dataurl|shareurl|inviteurl|vncurl|refresh|access/i.test(key);
}

function sanitizeString(value, literals) {
  let text = String(value);
  for (const literal of literals) {
    if (literal.length >= 4) text = text.replaceAll(literal, "<redacted>");
  }
  return text
    .replace(/\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer <redacted>")
    .replace(/\b(?:eyJ[A-Za-z0-9_-]{10,}|gbr_local_|cog_|ghp_|github_pat_|sk-)[A-Za-z0-9._~+/-]{8,}\b/g, "<redacted>")
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "<redacted>");
}

function sanitize(value, literals, key = "") {
  if (sensitiveKey(key)) return "<redacted>";
  if (typeof value === "string") return sanitizeString(value, literals);
  if (Array.isArray(value)) return value.map((item) => sanitize(item, literals));
  if (!isObject(value)) return value;
  return Object.fromEntries(Object.entries(value).map(([childKey, child]) => [
    childKey,
    sanitize(child, literals, childKey),
  ]));
}

function recordSetup(ctx, name, startedAtMs, response = {}) {
  ctx.setup.push({
    name,
    startedAtMs,
    endedAtMs: Date.now(),
    durationMs: Date.now() - startedAtMs,
    ...sanitize(response, ctx.literals),
  });
}

async function call(ctx, command, body = {}, { record = true } = {}) {
  const startedAtMs = Date.now();
  let status = 0;
  let parsed = null;
  let error;
  try {
    const response = await gatewayRequest(
      ctx.gateway,
      "POST",
      `/api/${command}`,
      { bearer: ctx.gateway.token, body },
    );
    status = response.status;
    parsed = parseText(await response.text());
  } catch (caught) {
    error = String(caught?.message ?? caught);
  }
  const entry = {
    command,
    request: { method: "POST", path: `/api/${command}`, body: sanitize(body, ctx.literals) },
    response: {
      status,
      body: sanitize(parsed, ctx.literals),
      ...(error ? { error: sanitizeString(error, ctx.literals) } : {}),
    },
    startedAtMs,
    endedAtMs: Date.now(),
    durationMs: Date.now() - startedAtMs,
  };
  if (record) ctx.caseRecord.calls.push(entry);
  return { status, body: parsed, entry, error };
}

function expectOk(response, label) {
  if (response.error || response.status < 200 || response.status >= 300) {
    throw new Error(`${label} returned HTTP ${response.status || "network-error"}${response.error ? `: ${response.error}` : ""}`);
  }
  return response.body;
}

async function waitFor(ctx, command, body, predicate, {
  timeoutMs = TIMEOUT_MS,
  intervalMs = POLL_MS,
} = {}) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    last = await call(ctx, command, body, { record: false });
    if (!last.error && last.status >= 200 && last.status < 300 && predicate(last.body)) {
      ctx.caseRecord.calls.push(last.entry);
      return last.body;
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, intervalMs));
  }
  if (last) ctx.caseRecord.calls.push(last.entry);
  throw new Error(`${command} did not satisfy the expected condition within ${timeoutMs}ms`);
}

function requireReady(ctx) {
  if (ctx.blockedReason) throw new BlockedError(ctx.blockedReason);
}

function requireAgent(ctx, name) {
  requireReady(ctx);
  const agent = ctx.agents[name];
  if (!agent?.id) throw new BlockedError(`required ${name} agent was not created`);
  return agent;
}

async function listAgents(ctx) {
  return listBody(expectOk(await call(ctx, "listAgents"), "listAgents"));
}

async function getAgentMemories(ctx, agentId) {
  return expectOk(await call(ctx, "getAgentMemories", { id: agentId }), "getAgentMemories");
}

async function createAgent(ctx, name, description, options = {}) {
  const clientNonce = options.clientNonce ?? `docs-conformance:${RUN_SUFFIX}:${name}`;
  const response = await call(ctx, "createAgent", {
    name,
    description,
    ...(options.title == null ? {} : { title: options.title }),
    origin: "api",
    clientNonce,
  });
  const body = expectOk(response, "createAgent");
  let id = extractId(body);
  if (!id) id = (await listAgents(ctx)).find((item) => item.name === name)?.id;
  if (!id) throw new Error(`createAgent returned no id for ${name}`);
  const agent = { id, name, description, clientNonce };
  ctx.createdAgents.set(id, agent);
  return agent;
}

async function getTranscript(ctx, agentId, record = true) {
  return expectOk(await call(ctx, "getAgentTranscript", { id: agentId }, { record }), "getAgentTranscript");
}

async function sendAndWait(ctx, agentId, prompt, marker, options = {}) {
  const startedAtMs = Date.now();
  expectOk(await call(ctx, "sendPrompt", {
    agentId,
    prompt,
    ...(options.attachmentPaths ? {
      attachmentPaths: options.attachmentPaths,
      attachmentNames: options.attachmentNames ?? [],
    } : {}),
  }), "sendPrompt");
  const transcript = await waitFor(
    ctx,
    "getAgentTranscript",
    { id: agentId },
    (body) => textIncludes(body, new RegExp(`\\b${escapeRegex(marker)}\\b`, "i")),
  );
  return { transcript, startedAtMs, endedAtMs: Date.now() };
}

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function findNamedRecord(ctx, command, agentId, name) {
  const body = expectOk(await call(ctx, command, { id: agentId }), command);
  const record = findByName(body, name);
  if (!record) throw new Error(`${command} did not list ${name}`);
  return record;
}

const cases = [
  {
    id: "named-bot",
    run: async (ctx) => {
      requireReady(ctx);
      const name = `Scout-${RUN_SUFFIX}`;
      const description = "Researches accounts";
      const nonce = `docs-conformance:${RUN_SUFFIX}:scout`;
      const scout = await createAgent(ctx, name, description, {
        title: "Research scout",
        clientNonce: nonce,
      });
      const listed = await listAgents(ctx);
      if (!listed.some((item) => item.id === scout.id && item.name === name && item.description === description)) {
        throw new Error("listAgents did not contain the created Scout with its description");
      }
      expectOk(await call(ctx, "updateAgent", {
        id: scout.id,
        profile: { name, description: "Researches accounts v2", title: "Research scout" },
      }), "updateAgent");
      const updated = await listAgents(ctx);
      const updatedScout = updated.find((item) => item.id === scout.id);
      if (updatedScout?.description !== "Researches accounts v2") {
        throw new Error("updateAgent description was not reflected in listAgents");
      }
      const duplicate = expectOk(await call(ctx, "createAgent", {
        name,
        description,
        title: "Research scout",
        origin: "api",
        clientNonce: nonce,
      }), "createAgent duplicate nonce");
      const duplicateId = extractId(duplicate);
      if (duplicateId !== scout.id) throw new Error("same clientNonce did not deduplicate to the same agent id");
      ctx.agents.scout = scout;
      return { id: scout.id, name, duplicateId };
    },
  },
  {
    id: "parallel-bots",
    run: async (ctx) => {
      requireReady(ctx);
      const a = await createAgent(ctx, `Bot-A-${RUN_SUFFIX}`, "Parallel bot A");
      const b = await createAgent(ctx, `Bot-B-${RUN_SUFFIX}`, "Parallel bot B");
      ctx.agents.a = a;
      ctx.agents.b = b;
      const [alpha, beta] = await Promise.all([
        sendAndWait(ctx, a.id, "Reply with exactly the single word alpha", "alpha"),
        sendAndWait(ctx, b.id, "Reply with exactly the single word beta", "beta"),
      ]);
      const overlap = Math.max(alpha.startedAtMs, beta.startedAtMs) <
        Math.min(alpha.endedAtMs, beta.endedAtMs);
      if (!overlap) throw new Error("parallel turn intervals did not overlap");
      return {
        botA: { id: a.id, startMs: alpha.startedAtMs, endMs: alpha.endedAtMs },
        botB: { id: b.id, startMs: beta.startedAtMs, endMs: beta.endedAtMs },
        overlap,
      };
    },
  },
  {
    id: "bot-to-bot",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      const b = requireAgent(ctx, "b");
      const enabled = expectOk(await call(ctx, "isAgentNetworkEnabled"), "isAgentNetworkEnabled");
      if (enabled !== true) throw new Error(`isAgentNetworkEnabled returned ${JSON.stringify(enabled)}`);
      const marker = `ping-${RUN_SUFFIX}`;
      expectOk(await call(ctx, "sendPrompt", {
        agentId: a.id,
        prompt: `Use your SendToAgent tool to send the bot named ${b.name} the message '${marker}'. Then reply done.`,
      }), "sendPrompt");
      const deadline = Date.now() + TIMEOUT_MS;
      let aTranscript;
      let bTranscript;
      let aEntry;
      let bEntry;
      while (Date.now() < deadline) {
        const [aResult, bResult] = await Promise.all([
          call(ctx, "getAgentTranscript", { id: a.id }, { record: false }),
          call(ctx, "getAgentTranscript", { id: b.id }, { record: false }),
        ]);
        aTranscript = aResult.body;
        bTranscript = bResult.body;
        aEntry = aResult.entry;
        bEntry = bResult.entry;
        const toolNames = findToolNames(aTranscript);
        if (hasMarker(bTranscript, marker) ||
            (hasMarkerInToolRecord(aTranscript, marker) && toolNames.length > 0)) {
          ctx.caseRecord.calls.push(aEntry, bEntry);
          return { delivered: true, toolNames, via: hasMarker(bTranscript, marker) ? "bot-b-transcript" : "bot-a-tool-record" };
        }
        await new Promise((resolvePromise) => setTimeout(resolvePromise, POLL_MS));
      }
      if (aEntry) ctx.caseRecord.calls.push(aEntry);
      if (bEntry) ctx.caseRecord.calls.push(bEntry);
      throw new Error("ping was not found in Bot B's transcript or Bot A's tool-call record");
    },
  },
  {
    id: "group",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      const b = requireAgent(ctx, "b");
      const groupName = `Team-${RUN_SUFFIX}`;
      const created = expectOk(await call(ctx, "createGroup", {
        name: groupName,
        memberAgentIds: [a.id, b.id],
      }), "createGroup");
      const groupId = extractId(created);
      if (!groupId) throw new Error("createGroup returned no group id");
      ctx.createdAgents.set(groupId, { id: groupId, name: groupName, isGroup: true });
      ctx.agents.group = { id: groupId, name: groupName };
      const listed = await listAgents(ctx);
      const group = listed.find((item) => item.id === groupId || item.name === groupName);
      const members = group?.memberIds ?? group?.memberAgentIds ?? group?.members;
      const memberCount = Array.isArray(members) ? members.length : group?.member_count;
      if (!group || memberCount !== 2) {
        throw new Error("listAgents did not show the group with two members");
      }
      const extras = [];
      for (let index = 0; index < 5; index += 1) {
        extras.push(await createAgent(ctx, `Group-extra-${index + 1}-${RUN_SUFFIX}`, "Group limit probe"));
      }
      const seven = [a, b, ...extras].map((item) => item.id);
      const sevenMemberResponse = await call(ctx, "setGroupMembers", {
        id: groupId,
        memberAgentIds: seven,
      });
      const sevenMemberResult = sevenMemberResponse.status >= 200 &&
        sevenMemberResponse.status < 300 && !sevenMemberResponse.error
        ? sevenMemberResponse.body
        : null;
      const updatedGroups = await listAgents(ctx);
      const updatedGroup = updatedGroups.find((item) =>
        item.id === groupId || item.name === groupName);
      const updatedMembers = updatedGroup?.memberIds ??
        updatedGroup?.memberAgentIds ??
        updatedGroup?.members ??
        sevenMemberResult?.memberIds ??
        sevenMemberResult?.memberAgentIds ??
        sevenMemberResult?.members;
      const truncatedMemberIds = Array.isArray(updatedMembers)
        ? updatedMembers.map((item) => typeof item === "string"
          ? item
          : item?.id ?? item?.agentId ?? item?.agent_id).filter(Boolean)
        : [];
      const limitError = sevenMemberResponse.error ||
        sevenMemberResponse.status < 200 ||
        sevenMemberResponse.status >= 300
        ? `setGroupMembers returned HTTP ${sevenMemberResponse.status || "network-error"}${sevenMemberResponse.error ? `: ${sevenMemberResponse.error}` : ""}`
        : !Array.isArray(updatedMembers)
          ? "setGroupMembers did not return the truncated member list"
          : truncatedMemberIds.length > 6
            ? `setGroupMembers returned ${truncatedMemberIds.length} members after a seven-member request`
            : null;
      const restored = await call(ctx, "setGroupMembers", { id: groupId, memberAgentIds: [a.id, b.id] });
      expectOk(restored, "setGroupMembers restore");
      const result = await sendAndWait(ctx, groupId, `@${a.name} reply with exactly the word groupok`, "groupok");
      if (limitError) throw new Error(limitError);
      return {
        groupId,
        memberCount: 2,
        sevenMemberStatus: sevenMemberResponse.status,
        truncatedMemberIds,
        groupok: Boolean(result.transcript),
      };
    },
  },
  {
    id: "memory",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      const marker = `teal-${RUN_SUFFIX}`;
      expectOk(await call(ctx, "sendPrompt", {
        agentId: a.id,
        prompt: `Save to your memory: my favorite color is ${marker}. Reply saved.`,
      }), "sendPrompt");
      const memories = await waitFor(ctx, "getAgentMemories", { id: a.id },
        (body) => listBody(body).some((item) => hasMarker(item, marker)));
      const memory = listBody(memories).find((item) => hasMarker(item, marker));
      const memoryId = memory?.id ?? memory?.memoryId;
      if (!memoryId) throw new Error("memory record had no id");
      expectOk(await call(ctx, "deleteAgentMemory", { id: a.id, memoryId }), "deleteAgentMemory");
      const after = await getAgentMemories(ctx, a.id);
      if (listBody(after).some((item) => hasMarker(item, marker))) throw new Error("deleted memory still appeared");
      return { memoryId, deleted: true };
    },
  },
  {
    id: "skill",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      const name = `summarize-${RUN_SUFFIX}`;
      const spec = {
        name,
        description: "use this when asked to summarize",
        body: "Reply with exactly SKILLOK",
        trigger: null,
      };
      const created = expectOk(await call(ctx, "createAgentWorkflow", { id: a.id, spec }), "createAgentWorkflow");
      const workflow = findByName(created, name) ?? await findNamedRecord(ctx, "getAgentWorkflows", a.id, name);
      const workflowId = extractId(workflow);
      if (!workflowId) throw new Error("workflow had no id");
      ctx.createdWorkflows.push({ agentId: a.id, workflowId });
      const listed = await findNamedRecord(ctx, "getAgentWorkflows", a.id, name);
      if (!listed) throw new Error("workflow was not listed");
      expectOk(await call(ctx, "runAgentWorkflowNow", { id: a.id, workflowId }), "runAgentWorkflowNow");
      await waitFor(ctx, "getAgentTranscript", { id: a.id }, (body) => textIncludes(body, /\bSKILLOK\b/i));
      expectOk(await call(ctx, "deleteAgentWorkflow", { id: a.id, workflowId }), "deleteAgentWorkflow");
      ctx.createdWorkflows = ctx.createdWorkflows.filter((item) => item.workflowId !== workflowId);
      const after = listBody(expectOk(await call(ctx, "getAgentWorkflows", { id: a.id }), "getAgentWorkflows"));
      if (after.some((item) => item.id === workflowId || item.name === name)) throw new Error("workflow remained after deletion");
      return { workflowId, deleted: true };
    },
  },
  {
    id: "routine-run",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      const name = `Daily-${RUN_SUFFIX}`;
      const spec = {
        name,
        prompt: "Reply with exactly ROUTINEOK",
        trigger: { type: "cron", schedule: "0 9 * * *" },
        isEnabled: true,
      };
      const created = expectOk(await call(ctx, "createAgentAutomation", { id: a.id, spec }), "createAgentAutomation");
      const automation = findByName(created, name) ?? await findNamedRecord(ctx, "getAgentAutomations", a.id, name);
      const automationId = extractId(automation);
      if (!automationId) throw new Error("automation had no id");
      ctx.createdAutomations.push({ agentId: a.id, automationId });
      expectOk(await call(ctx, "runAgentAutomationNow", { id: a.id, automationId }), "runAgentAutomationNow");
      await waitFor(ctx, "getAgentTranscript", { id: a.id }, (body) => textIncludes(body, /\bROUTINEOK\b/i));
      const afterRun = await findNamedRecord(ctx, "getAgentAutomations", a.id, name);
      if (!Array.isArray(afterRun.runs) || afterRun.runs.length < 1) throw new Error("automation had no run record");
      const disabledSpec = { ...spec, isEnabled: false };
      const updated = expectOk(await call(ctx, "updateAgentAutomation", {
        id: a.id,
        automationId,
        spec: disabledSpec,
      }), "updateAgentAutomation");
      const disabled = findByName(updated, name) ?? await findNamedRecord(ctx, "getAgentAutomations", a.id, name);
      if (disabled.isEnabled !== false) throw new Error("automation did not reflect enabled=false");
      expectOk(await call(ctx, "deleteAgentAutomation", { id: a.id, automationId }), "deleteAgentAutomation");
      ctx.createdAutomations = ctx.createdAutomations.filter((item) => item.automationId !== automationId);
      return { automationId, runCount: afterRun.runs.length, disabled: true, deleted: true };
    },
  },
  {
    id: "routine-unattended",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      const marker = `TICK-${RUN_SUFFIX}`;
      const name = `Unattended-${RUN_SUFFIX}`;
      const spec = {
        name,
        prompt: `Reply with exactly ${marker}`,
        trigger: { type: "cron", schedule: "* * * * *" },
        isEnabled: true,
      };
      const created = expectOk(await call(ctx, "createAgentAutomation", { id: a.id, spec }), "createAgentAutomation");
      const automation = findByName(created, name) ?? await findNamedRecord(ctx, "getAgentAutomations", a.id, name);
      const automationId = extractId(automation);
      if (!automationId) throw new Error("unattended automation had no id");
      ctx.createdAutomations.push({ agentId: a.id, automationId });
      const silentWindowStartedAtMs = Date.now();
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 150_000));
      const transcript = await getTranscript(ctx, a.id);
      const startedAtMs = findAssistantMarkerTimestamp(transcript, marker);
      if (startedAtMs == null ||
          startedAtMs < silentWindowStartedAtMs ||
          startedAtMs > Date.now()) {
        throw new Error(`no ${marker} turn started during the silent window`);
      }
      expectOk(await call(ctx, "deleteAgentAutomation", { id: a.id, automationId }), "deleteAgentAutomation");
      ctx.createdAutomations = ctx.createdAutomations.filter((item) => item.automationId !== automationId);
      return {
        automationId,
        silentWindowStartedAtMs,
        silentWindowEndedAtMs: Date.now(),
        turnStartedAtMs: startedAtMs,
        eventsStreamsOpenedByHarness: 0,
      };
    },
  },
  {
    id: "attachment",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      const marker = `zebra-${RUN_SUFFIX}`;
      const filename = `docs-conformance-${RUN_SUFFIX}.txt`;
      const uploaded = expectOk(await call(ctx, "uploadAttachment", {
        filename,
        bytesBase64: Buffer.from(marker, "utf8").toString("base64"),
        agentId: a.id,
      }), "uploadAttachment");
      const path = uploaded?.path;
      if (typeof path !== "string") throw new Error("uploadAttachment returned no path");
      const read = expectOk(await call(ctx, "readAttachmentText", { path, agentId: a.id }), "readAttachmentText");
      if (!hasMarker(read, marker)) throw new Error("readAttachmentText did not return uploaded text");
      let computerBootingObserved = false;
      for (let attempt = 1; attempt <= 2; attempt += 1) {
        try {
          const response = await sendAndWait(ctx, a.id, "What word is in the attached file? reply with only it", marker, {
            attachmentPaths: [path],
            attachmentNames: [filename],
          });
          computerBootingObserved ||= /computer(?:\s+is)?\s+booting/i.test(collectAssistantText(response.transcript).join("\n"));
          return {
            path,
            attachmentTextMatched: true,
            transcriptMatched: Boolean(response.transcript),
            attempts: attempt,
            computerBootingObserved,
          };
        } catch (error) {
          if (!/getAgentTranscript did not satisfy/.test(error?.message ?? "")) throw error;
          const transcript = await getTranscript(ctx, a.id).catch(() => undefined);
          computerBootingObserved ||= /computer(?:\s+is)?\s+booting/i.test(collectAssistantText(transcript).join("\n"));
          if (attempt === 2) {
            throw new Error(`${error?.message ?? error}; computerBootingObserved=${computerBootingObserved}; attempts=${attempt}`);
          }
        }
      }
      throw new Error("attachment transcript polling exhausted its 180-second attempts");
    },
  },
  {
    id: "search",
    run: async (ctx) => {
      const botB = requireAgent(ctx, "b");
      const marker = `ping-${RUN_SUFFIX}`;
      const search = await call(ctx, "searchAgents", { query: marker, limit: 20 });
      const agents = !search.error && search.status >= 200 && search.status < 300
        ? listBody(search.body)
        : [];
      const agentFound = agents.some((item) =>
        item.id === botB.id || item.agentId === botB.id);
      const media = await call(ctx, "searchMedia", { query: marker, limit: 20 });
      expectOk(media, "searchMedia");
      if (search.error || search.status < 200 || search.status >= 300) {
        throw new Error(`searchAgents returned HTTP ${search.status || "network-error"}`);
      }
      if (!agentFound) throw new Error(`searchAgents did not return Bot B (${botB.id}) for ${marker}`);
      return {
        query: marker,
        botBId: botB.id,
        agentFound,
        mediaReturned: true,
        mediaCount: listBody(media.body).length,
      };
    },
  },
  {
    id: "secret-request",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      const label = `DEMO_TOKEN_${RUN_SUFFIX}`;
      const secretValue = `s3cr3t-${RUN_SUFFIX}`;
      ctx.literals.add(secretValue);
      expectOk(await call(ctx, "sendPrompt", {
        agentId: a.id,
        prompt: `Ask me for a secret named ${label} using your secret request tool, then wait.`,
      }), "sendPrompt");
      const transcript = await waitFor(ctx, "getAgentTranscript", { id: a.id }, (body) => Boolean(findSecretRequest(body)));
      const request = findSecretRequest(transcript);
      if (!request) throw new Error("transcript did not contain a secret-request widget/entry");
      const beforeStatus = await call(ctx, "getBoxSecretsStatus");
      expectOk(beforeStatus, "getBoxSecretsStatus");
      expectOk(await call(ctx, "submitSecret", {
        entryId: request.id,
        value: secretValue,
        agentId: a.id,
      }), "submitSecret");
      const afterSubmit = await getTranscript(ctx, a.id);
      if (hasMarker(afterSubmit, secretValue)) throw new Error("submitted secret appeared in transcript");
      const followup = await sendAndWait(ctx, a.id, "Reply with exactly SECRETPOSTOK", "SECRETPOSTOK");
      if (hasMarker(followup.transcript, secretValue)) throw new Error("submitted secret appeared in a later response");
      const afterStatus = await call(ctx, "getBoxSecretsStatus");
      expectOk(afterStatus, "getBoxSecretsStatus");
      return { secretRequestEntryId: request.id, secretValueAbsent: true, beforeStatus: beforeStatus.body, afterStatus: afterStatus.body };
    },
  },
  {
    id: "notifications",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      const initial = (await listAgents(ctx)).find((item) => item.id === a.id);
      if (!initial) throw new Error("Bot A missing from listAgents");
      const originalUnread = Boolean(initial.hasUnread ?? initial.isUnread);
      const originalNotify = Boolean(
        initial.notifyOnUpdatesEnabled ??
        initial.notifyOnAgentUpdates ??
        initial.notifyOnUpdates,
      );
      const originalHidden = Boolean(initial.hiddenFromSidebar ?? initial.isHiddenFromSidebar);
      const changes = [
        ["setAgentUnread", { id: a.id, isUnread: !originalUnread, atMs: Date.now() }, "unread", !originalUnread],
        ["setAgentNotifyOnUpdates", { id: a.id, isEnabled: !originalNotify }, "notify", !originalNotify],
        ["setAgentHiddenFromSidebar", { id: a.id, isHidden: !originalHidden }, "hiddenFromSidebar", !originalHidden],
      ];
      let failure;
      for (const [command, body, field, expected] of changes) {
        const changed = await call(ctx, command, body);
        if (changed.error || changed.status < 200 || changed.status >= 300) {
          failure ??= `${command} returned HTTP ${changed.status || "network-error"}`;
          continue;
        }
        let row;
        try {
          row = (await listAgents(ctx)).find((item) => item.id === a.id);
        } catch (error) {
          failure ??= `${command} verification failed: ${error?.message ?? error}`;
          continue;
        }
        const actual = field === "notify"
          ? (row?.notifyOnUpdatesEnabled ?? row?.notifyOnAgentUpdates ?? row?.notifyOnUpdates)
          : field === "hiddenFromSidebar"
            ? (row?.hiddenFromSidebar ?? row?.isHiddenFromSidebar)
            : (row?.hasUnread ?? row?.isUnread);
        if (actual !== expected) failure ??= `${command} was not reflected in listAgents`;
      }
      for (const [command, body] of [
        ["setAgentUnread", { id: a.id, isUnread: originalUnread }],
        ["setAgentNotifyOnUpdates", { id: a.id, isEnabled: originalNotify }],
        ["setAgentHiddenFromSidebar", { id: a.id, isHidden: originalHidden }],
      ]) {
        const restored = await call(ctx, command, body);
        if (restored.error || restored.status < 200 || restored.status >= 300) {
          failure ??= `${command} restore returned HTTP ${restored.status || "network-error"}`;
        }
      }
      if (failure) throw new Error(failure);
      return { unread: true, notifyOnUpdates: true, hiddenFromSidebar: true, restored: true };
    },
  },
  {
    id: "settings",
    run: async (ctx) => {
      requireReady(ctx);
      const original = expectOk(await call(ctx, "getHostSettings"), "getHostSettings");
      const originalOverride = original?.userTimeZoneOverride;
      const replacement = originalOverride === "Etc/UTC" ? "UTC" : "Etc/UTC";
      expectOk(await call(ctx, "setHostSettings", { userTimeZoneOverride: replacement }), "setHostSettings");
      const changed = expectOk(await call(ctx, "getHostSettings"), "getHostSettings");
      if (changed?.userTimeZoneOverride !== replacement) throw new Error("settings round-trip did not reflect replacement");
      expectOk(await call(ctx, "setHostSettings", { userTimeZoneOverride: originalOverride ?? "" }), "setHostSettings restore");
      const restored = expectOk(await call(ctx, "getHostSettings"), "getHostSettings restore");
      if (originalOverride == null ? restored?.userTimeZoneOverride != null : restored?.userTimeZoneOverride !== originalOverride) {
        throw new Error("settings restore did not return the original value");
      }
      return { field: "userTimeZoneOverride", replacement, restored: true };
    },
  },
  {
    id: "box-status",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      expectOk(await call(ctx, "ensureForeverBox", { id: a.id }), "ensureForeverBox");
      const body = expectOk(await call(ctx, "getForeverBoxStatus", { id: a.id }), "getForeverBoxStatus");
      if (body?.state !== "running" || typeof body?.vncUrl !== "string" || body.vncUrl.length === 0) {
        throw new Error(`box status did not report running with a VNC URL: ${JSON.stringify({
          state: body?.state,
          hasVncUrl: typeof body?.vncUrl === "string" && body.vncUrl.length > 0,
          diskPressure: body?.diskPressure,
        })}`);
      }
      return { ready: true, diskPressure: body.diskPressure, status: body };
    },
  },
  {
    id: "teach",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      const initial = expectOk(await call(ctx, "getTeachRecordingStatus"), "getTeachRecordingStatus");
      if (initial?.state !== "idle") throw new Error(`teach recording was not idle: ${JSON.stringify(initial)}`);
      let started = false;
      try {
        const start = await call(ctx, "startTeachRecording", { agentId: a.id, entryPoint: "docs-conformance" });
        if (start.status < 200 || start.status >= 300) {
          const detail = JSON.stringify(start.body ?? start.error);
          if (/renderer|monitor|desktop|no_monitor|private/i.test(detail)) {
            throw new BlockedError(`teach recording requires an available renderer/monitor: ${detail}`);
          }
          throw new Error(`startTeachRecording returned HTTP ${start.status}: ${detail}`);
        }
        started = true;
        const recording = expectOk(start, "startTeachRecording");
        if (recording?.state !== "recording") throw new Error(`startTeachRecording did not enter recording state: ${JSON.stringify(recording)}`);
        const during = expectOk(await call(ctx, "getTeachRecordingStatus"), "getTeachRecordingStatus");
        if (during?.state !== "recording") throw new Error("teach recording status did not show recording");
        const stopped = expectOk(await call(ctx, "stopTeachRecording", { agentId: a.id, save: false }), "stopTeachRecording");
        started = false;
        if (stopped?.state !== "idle") throw new Error("stopTeachRecording did not return idle");
        return { initial: initial.state, during: during.state, final: stopped.state };
      } catch (error) {
        if (started) await call(ctx, "stopTeachRecording", { agentId: a.id, save: false });
        throw error;
      }
    },
  },
  {
    id: "plugins",
    run: async (ctx) => {
      requireReady(ctx);
      const catalog = expectOk(await call(ctx, "skillsCatalog"), "skillsCatalog");
      const sync = expectOk(await call(ctx, "getPluginSyncStatus"), "getPluginSyncStatus");
      const servers = expectOk(await call(ctx, "listBoxMcpServers", { serverIdentifiers: [] }), "listBoxMcpServers");
      const entries = listBody(catalog);
      return { catalogEmpty: entries.length === 0, catalogCount: entries.length, sync, servers };
    },
  },
  {
    id: "sharing",
    run: async (ctx) => {
      const a = requireAgent(ctx, "a");
      expectOk(await call(ctx, "getSharingState"), "getSharingState");
      const created = expectOk(await call(ctx, "createRoomFromAgent", { agentId: a.id }), "createRoomFromAgent");
      const roomId = created?.roomId;
      if (typeof roomId !== "string") throw new Error("createRoomFromAgent returned no roomId");
      if (typeof created?.shareUrl !== "string" || created.shareUrl.length === 0) {
        throw new Error("createRoomFromAgent returned no shareUrl");
      }
      ctx.createdRooms.add(roomId);
      const state = expectOk(await call(ctx, "getSharingState"), "getSharingState after create");
      const rooms = Array.isArray(state?.rooms) ? state.rooms : [];
      if (!rooms.some((room) => room?.roomId === roomId)) {
        throw new Error("getSharingState did not include the created room");
      }
      const invite = expectOk(await call(ctx, "createRoomInvite", { roomId }), "createRoomInvite");
      if (invite?.status !== "ok" || typeof invite.shareUrl !== "string") {
        throw new Error("createRoomInvite did not return an invite URL");
      }
      return { roomId, shareUrlPresent: true, roomVisibleInState: true, inviteReturned: true, inviteUrlPresent: true };
    },
  },
  {
    id: "delete",
    run: async (ctx) => {
      const scout = requireAgent(ctx, "scout");
      expectOk(await call(ctx, "deleteAgent", { id: scout.id }), "deleteAgent");
      ctx.deletedAgents.add(scout.id);
      ctx.createdAgents.delete(scout.id);
      const listed = await listAgents(ctx);
      if (listed.some((item) => item.id === scout.id || item.name === scout.name)) {
        throw new Error("deleted Scout still appeared in listAgents");
      }
      return { deletedId: scout.id, absentFromList: true };
    },
  },
];

async function runCase(ctx, definition) {
  const startedAtMs = Date.now();
  const caseRecord = {
    id: definition.id,
    matrixRow: matrixRows[definition.id],
    status: "FAIL",
    startedAtMs,
    calls: [],
  };
  ctx.caseRecord = caseRecord;
  try {
    caseRecord.result = await definition.run(ctx);
    caseRecord.status = "PASS";
  } catch (error) {
    caseRecord.status = error instanceof BlockedError ? "BLOCKED" : "FAIL";
    caseRecord.error = sanitizeString(error?.message ?? error, ctx.literals);
  }
  caseRecord.endedAtMs = Date.now();
  caseRecord.durationMs = caseRecord.endedAtMs - startedAtMs;
  await writeFile(
    resolve(OUT, `${definition.id}.json`),
    JSON.stringify(sanitize(caseRecord, ctx.literals), null, 2),
  );
  console.log(`${caseRecord.status} ${definition.id} (${caseRecord.durationMs}ms)`);
  return caseRecord;
}

async function cleanup(ctx) {
  const cleanup = [];
  const runCleanup = async (command, body) => {
    const startedAtMs = Date.now();
    const response = await call(ctx, command, body, { record: false });
    cleanup.push({
      command,
      request: { body: sanitize(body, ctx.literals) },
      response: sanitize({ status: response.status, body: response.body, error: response.error }, ctx.literals),
      startedAtMs,
      endedAtMs: Date.now(),
      durationMs: Date.now() - startedAtMs,
    });
  };
  for (const item of [...ctx.createdWorkflows]) {
    await runCleanup("deleteAgentWorkflow", { id: item.agentId, workflowId: item.workflowId });
  }
  for (const item of [...ctx.createdAutomations]) {
    await runCleanup("deleteAgentAutomation", { id: item.agentId, automationId: item.automationId });
  }
  for (const roomId of ctx.createdRooms) {
    await runCleanup("leaveSharedRoom", { roomId });
  }
  for (const id of [...ctx.createdAgents.keys()].reverse()) {
    if (!ctx.deletedAgents.has(id)) await runCleanup("deleteAgent", { id });
  }
  return cleanup;
}

await mkdir(OUT, { recursive: true });
const ctx = {
  literals: new Set(),
  setup: [],
  agents: {},
  createdAgents: new Map(),
  createdWorkflows: [],
  createdAutomations: [],
  createdRooms: new Set(),
  deletedAgents: new Set(),
  caseRecord: null,
  gateway: null,
  blockedReason: null,
};
let session;
try {
  const step = async (name, fn, extraFn) => {
    const startedAtMs = Date.now();
    try {
      const body = await fn();
      const extra = extraFn ? extraFn(body) : {};
      recordSetup(ctx, name, startedAtMs, { status: "pass", ...extra });
      return body;
    } catch (error) {
      recordSetup(ctx, name, startedAtMs, {
        status: "fail",
        error: sanitizeString(error?.message ?? error, ctx.literals),
      });
      return undefined;
    }
  };
  session = await connectDevboxSession({ origin: ORIGIN, apiKey: API_KEY, step });
  if (!session) {
    ctx.blockedReason = "desktop authentication/Connect session could not be established";
  } else {
    const box = await ensureDevboxSandbox(session.grokBot, step);
    if (!box) {
      ctx.blockedReason = "EnsureSandBox did not return a usable box session";
    } else if (!String(box.podId).startsWith(EXPECTED_BOX_PREFIX)) {
      ctx.blockedReason = `EnsureSandBox returned unexpected box ${String(box.podId).slice(0, 16)}; expected ${EXPECTED_BOX_PREFIX}`;
    } else {
      ctx.gateway = createDevboxGateway(box);
      const startedAtMs = Date.now();
      try {
        const response = await gatewayRequest(ctx.gateway, "GET", "/health");
        const body = parseText(await response.text());
        recordSetup(ctx, "gateway /health", startedAtMs, {
          status: response.status === 200 ? "pass" : "fail",
          httpStatus: response.status,
          body,
        });
        if (response.status !== 200) ctx.blockedReason = `gateway health returned HTTP ${response.status}`;
      } catch (error) {
        recordSetup(ctx, "gateway /health", startedAtMs, {
          status: "fail",
          error: sanitizeString(error?.message ?? error, ctx.literals),
        });
        ctx.blockedReason = "gateway health request failed";
      }
    }
  }
} catch (error) {
  ctx.blockedReason = sanitizeString(error?.message ?? error, ctx.literals);
}

const results = [];
try {
  for (const definition of cases) results.push(await runCase(ctx, definition));
} finally {
  if (ctx.gateway) {
    ctx.caseRecord = { calls: [] };
    const cleanupResults = await cleanup(ctx);
    ctx.cleanup = cleanupResults;
  } else {
    ctx.cleanup = [];
  }
}

const counts = Object.fromEntries(["PASS", "FAIL", "BLOCKED"].map((status) => [
  status,
  results.filter((item) => item.status === status).length,
]));
const summary = {
  runSuffix: RUN_SUFFIX,
  expectedBoxPrefix: EXPECTED_BOX_PREFIX,
  setup: sanitize(ctx.setup, ctx.literals),
  setupBlockedReason: ctx.blockedReason,
  cases: results.map((item) => ({
    id: item.id,
    matrixRow: item.matrixRow,
    status: item.status,
    durationMs: item.durationMs,
    ...(item.error ? { error: item.error } : {}),
  })),
  counts,
  cleanup: sanitize(ctx.cleanup, ctx.literals),
  generatedAt: new Date().toISOString(),
};
await writeFile(resolve(OUT, "summary.json"), JSON.stringify(summary, null, 2));
console.log(`Summary PASS=${counts.PASS} FAIL=${counts.FAIL} BLOCKED=${counts.BLOCKED}`);
process.exitCode = counts.FAIL > 0 ? 1 : 0;
