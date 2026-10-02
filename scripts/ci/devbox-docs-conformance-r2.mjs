import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { isDeepStrictEqual } from "node:util";

import {
  collectAssistantText,
  connectDevboxSession,
  createDevboxGateway,
  ensureDevboxSandbox,
  gatewayRequest,
} from "./devbox-backend-smoke-helpers.mjs";

const ORIGIN = process.env.DEVBOX_ORIGIN ?? "https://app.devinai.net";
const API_KEY = process.env.DEVBOX_API_KEY ?? "";
const CONTROL_PLANE_ORIGIN = process.env.DEVBOX_CONTROL_PLANE_ORIGIN ??
  "http://127.0.0.1:8000";
const ORGANIZATION_ID = "org-5cc24fac78f946c4bef43e452af89f0e";
const OUT = resolve(process.argv.includes("--out")
  ? process.argv[process.argv.indexOf("--out") + 1]
  : process.argv[2] ?? "devbox-docs-conformance-r2");
const EXPECTED_BOX_PREFIX = "a8dce654";
const RUN_SUFFIX = `${Date.now().toString(36)}-${randomUUID().slice(0, 8)}`;
const TIMEOUT_MS = 180_000;
const POLL_MS = 2_000;
const GUEST_SESSION_ID = process.env.P23_SESSION_ID ??
  "devin-a8dce6544e8c4e279a4349c7d6fc4cfd";
const AGENT_LIMIT = 50;
const AUTOMATION_LIMIT = 50;
const MIN_FREE_BYTES = 700 * 1024 * 1024;

const matrixRows = {
  "bot-cap-50": "Create/edit Bots, organize the sidebar, and support up to 50 Bots ([Work])",
  "routine-cap-50": "Test/manage routines; each Bot can have up to 50, with the latest 20 runs retained ([Work])",
  "routine-run-history-20": "Test/manage routines; each Bot can have up to 50, with the latest 20 runs retained ([Work])",
  "delete-state": "Delete a Bot and remove/retain associated state as documented ([Work])",
  "secret-masking": "Request secrets securely and keep values masked from logs/model context ([Work])",
  "search-cross-bot": "Search prior conversations, files, links, and routines across Bots ([Work])",
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
    value.automation?.id,
    value.id,
    value.agentId,
    value.agent_id,
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
  for (const key of [
    "agents",
    "automations",
    "memories",
    "items",
    "results",
    "entries",
    "transcript",
  ]) {
    if (Array.isArray(value[key])) return value[key];
  }
  return [];
}

function agentName(value) {
  return value?.name ?? value?.profile?.name;
}

function agentDescription(value) {
  return value?.description ?? value?.profile?.description;
}

function findByName(value, name) {
  return listBody(value).find((item) =>
    item?.name === name || item?.profile?.name === name);
}

function textIncludes(value, pattern) {
  return collectAssistantText(value).some((text) => pattern.test(text));
}

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
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
    request: {
      method: "POST",
      path: `/api/${command}`,
      body: sanitize(body, ctx.literals),
    },
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

function addProjectedCall(ctx, response, body) {
  ctx.caseRecord.calls.push({
    ...response.entry,
    response: {
      ...response.entry.response,
      body: sanitize(body, ctx.literals),
    },
  });
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

function requireAgent(ctx, key) {
  requireReady(ctx);
  const agent = ctx.agents[key];
  if (!agent?.id) throw new BlockedError(`required ${key} agent was not created`);
  return agent;
}

async function listAgents(ctx) {
  return listBody(expectOk(await call(ctx, "listAgents"), "listAgents"));
}

async function getAutomations(ctx, agentId, { record = true } = {}) {
  return listBody(expectOk(
    await call(ctx, "getAgentAutomations", { id: agentId }, { record }),
    "getAgentAutomations",
  ));
}

async function createAgent(ctx, name, description, { exactHttp200 = false } = {}) {
  const clientNonce = `docs-conformance-r2:${RUN_SUFFIX}:${name}`;
  const response = await call(ctx, "createAgent", {
    name,
    description,
    origin: "api",
    clientNonce,
  });
  if (exactHttp200 && response.status !== 200) {
    throw new Error(`createAgent returned HTTP ${response.status}, expected 200`);
  }
  const body = expectOk(response, "createAgent");
  const id = extractId(body) ??
    (await listAgents(ctx)).find((item) => agentName(item) === name)?.id;
  if (!id) throw new Error(`createAgent returned no id for ${name}`);
  const agent = { id, name, description, clientNonce };
  ctx.createdAgents.set(id, agent);
  return agent;
}

async function sendAndWait(ctx, agentId, prompt, marker, options = {}) {
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
  return transcript;
}

async function findNamedRecord(ctx, command, agentId, name) {
  const body = expectOk(await call(ctx, command, { id: agentId }), command);
  const record = findByName(body, name);
  if (!record) throw new Error(`${command} did not list ${name}`);
  return record;
}

function automationById(records, id) {
  return records.find((record) => record?.id === id);
}

function automationSpec(name, prompt, isEnabled = false) {
  return {
    name,
    prompt,
    trigger: { type: "cron", schedule: "0 9 * * *" },
    isEnabled,
  };
}

function appendNotCovered(ctx, subcheck, search, findings) {
  ctx.caseRecord.notCovered ??= [];
  ctx.caseRecord.notCovered.push({
    subcheck,
    status: "NOT_COVERED",
    search,
    findings,
  });
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

function guestExec(ctx, command, timeoutSec = 120, { record = true } = {}) {
  const startedAtMs = Date.now();
  let stdout = "";
  let stderr = "";
  let error;
  const invocation = [
    "cd /home/ubuntu/repos/DevBox",
    "set -a; . tools/devbox-env.sh >/dev/null 2>&1; . ~/.devbox/node-secrets.env >/dev/null 2>&1; set +a",
    `.venv/bin/python tools/p23_guest_exec.py ${shellQuote(GUEST_SESSION_ID)} ${shellQuote(command)} ${timeoutSec}`,
  ].join("\n");
  try {
    stdout = execFileSync("bash", ["-lc", invocation], {
      cwd: "/home/ubuntu/repos/DevBox",
      encoding: "utf8",
      timeout: timeoutSec * 1000 + 15_000,
      maxBuffer: 4 * 1024 * 1024,
      env: process.env,
    });
  } catch (caught) {
    stdout = String(caught?.stdout ?? "");
    stderr = String(caught?.stderr ?? "");
    error = String(caught?.message ?? caught);
  }
  const exitMatch = stdout.match(/(?:^|\n)exit=(-?\d+)/);
  const exitCode = exitMatch ? Number(exitMatch[1]) : null;
  const output = stdout.replace(/(?:^|\n)exit=-?\d+\s*/, "").trim();
  const entry = {
    command: "p23_guest_exec",
    request: {
      method: "EXEC",
      sessionId: GUEST_SESSION_ID,
      command,
      timeoutSec,
    },
    response: {
      exitCode,
      output: sanitizeString(output, ctx.literals),
      ...(stderr ? { stderr: sanitizeString(stderr, ctx.literals) } : {}),
      ...(error ? { error: sanitizeString(error, ctx.literals) } : {}),
    },
    startedAtMs,
    endedAtMs: Date.now(),
    durationMs: Date.now() - startedAtMs,
  };
  if (record && ctx.caseRecord) ctx.caseRecord.calls.push(entry);
  return { exitCode, output, entry, error };
}

function parseAvailableBytes(output) {
  const lines = output.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const available = Number(lines.at(-1));
  return Number.isFinite(available) ? available : null;
}

function agentInventoryCount(agents) {
  return agents.length;
}

function isNotFoundOrEmpty(response) {
  const raw = JSON.stringify(response.body ?? "");
  if (response.status === 404 || response.status === 410) return true;
  if (response.status >= 400 && /not found|no such|missing|does not exist/i.test(raw)) {
    return true;
  }
  if (response.status < 200 || response.status >= 300) return false;
  return response.body == null || listBody(response.body).length === 0;
}

function countNamedTranscriptTurns(transcript, marker) {
  return listBody(transcript)
    .filter((entry) => entry?.kind !== "event" && JSON.stringify(entry).includes(marker))
    .length;
}

function countExactAssistantText(transcript, text) {
  return collectAssistantText(transcript).filter((value) => value.trim() === text).length;
}

function assistantSendMessageIds(transcript) {
  return listBody(transcript)
    .filter((entry) => entry?.kind === "send-message" && typeof entry.id === "string")
    .map((entry) => entry.id);
}

function hostSettingsProjection(value) {
  return {
    pinnedAgentIds: Array.isArray(value?.pinnedAgentIds) ? value.pinnedAgentIds : [],
    sidebarSections: Array.isArray(value?.sidebarSections) ? value.sidebarSections : [],
  };
}

async function readOrgBoxInventory(ctx) {
  const path = `/v3/organizations/${ORGANIZATION_ID}/sessions?first=200`;
  try {
    const response = await fetch(`${CONTROL_PLANE_ORIGIN}${path}`, {
      headers: {
        accept: "application/json",
        authorization: `Bearer ${API_KEY}`,
      },
    });
    const body = await response.json();
    const sessions = Array.isArray(body?.items) ? body.items : [];
    const boxes = sessions
      .filter((session) => Array.isArray(session.tags) &&
        session.tags.includes("grok-bot-box"))
      .map((session) => ({
        sessionIdPrefix: String(
          session.session_id ?? session.devin_id ?? session.id ?? "",
        ).slice(0, 8),
        status: session.status ?? null,
        tags: session.tags,
      }));
    return {
      httpStatus: response.status,
      orgId: ORGANIZATION_ID,
      totalSessions: body?.total ?? sessions.length,
      boxTag: "grok-bot-box",
      taggedBoxCount: boxes.length,
      runningBoxCount: boxes.filter((box) => box.status === "running").length,
      boxes,
    };
  } catch (error) {
    return {
      httpStatus: 0,
      orgId: ORGANIZATION_ID,
      boxTag: "grok-bot-box",
      taggedBoxCount: 0,
      runningBoxCount: 0,
      boxes: [],
      error: sanitizeString(error?.message ?? error, ctx.literals),
    };
  }
}

function safeAgentInventory(agents) {
  return agents.map((agent) => ({
    id: agent.id,
    name: agentName(agent) ?? null,
    createdAt: agent.createdAt ?? null,
    isGroup: agent.isGroup === true,
    isHiddenFromSidebar: agent.isHiddenFromSidebar === true,
  })).sort((left, right) => String(left.id).localeCompare(String(right.id)));
}

const cases = [
  {
    id: "bot-cap-50",
    run: async (ctx) => {
      requireReady(ctx);
      const dfBefore = guestExec(ctx, "df -B1 --output=avail /", 60);
      if (dfBefore.exitCode !== 0) {
        throw new BlockedError("guest df pre-check failed; no cap Bots were created");
      }
      const freeBytesBefore = parseAvailableBytes(dfBefore.output);
      if (freeBytesBefore == null) {
        throw new BlockedError("guest df pre-check did not return available bytes");
      }
      ctx.guestDfBefore = { availableBytes: freeBytesBefore };
      if (freeBytesBefore < MIN_FREE_BYTES) {
        throw new BlockedError(`guest / has ${freeBytesBefore} bytes free, below the 700 MiB threshold`);
      }

      requireReady(ctx);
      const before = await listAgents(ctx);
      const initialCount = agentInventoryCount(before);
      if (initialCount >= AGENT_LIMIT) {
        throw new BlockedError(`listAgents already reports ${initialCount} owned agents; no room for cap setup`);
      }

      const created = [];
      for (let index = 0; index < AGENT_LIMIT - initialCount; index += 1) {
        const name = `Cap-${RUN_SUFFIX}-${index + 1}`;
        created.push(await createAgent(ctx, name, `R2 cap test ${index + 1}`, {
          exactHttp200: true,
        }));
      }
      const atCap = await listAgents(ctx);
      if (agentInventoryCount(atCap) !== AGENT_LIMIT) {
        throw new Error(`listAgents reported ${agentInventoryCount(atCap)} agents after cap setup, expected 50`);
      }

      const overflowName = `Cap-${RUN_SUFFIX}-overflow`;
      const overflow = await call(ctx, "createAgent", {
        name: overflowName,
        description: "Must be rejected at the 50-Bot cap",
        origin: "api",
        clientNonce: `docs-conformance-r2:${RUN_SUFFIX}:overflow`,
      });
      const overflowBody = JSON.stringify(overflow.body ?? "");
      const overflowRejected = !overflow.error &&
        (overflow.status < 200 || overflow.status >= 300);
      if (!overflowRejected ||
          !/50/.test(overflowBody) ||
          !/limit|maximum|max/i.test(overflowBody)) {
        throw new Error(`51st createAgent was not rejected with the 50-Bot limit (HTTP ${overflow.status})`);
      }

      const edited = created[0];
      const editedName = `Cap-Edited-${RUN_SUFFIX}`;
      const editedDescription = `R2 cap profile ${RUN_SUFFIX}`;
      expectOk(await call(ctx, "updateAgent", {
        id: edited.id,
        profile: { name: editedName, description: editedDescription },
      }), "updateAgent");
      const afterEdit = (await listAgents(ctx)).find((item) => item.id === edited.id);
      if (agentName(afterEdit) !== editedName ||
          agentDescription(afterEdit) !== editedDescription) {
        throw new Error("updateAgent name and description were not reflected by listAgents");
      }
      edited.name = editedName;
      edited.description = editedDescription;

      const hideMarker = `HIDECAPOK-${RUN_SUFFIX}`;
      const hiddenRoutineName = `HideRoutine-${RUN_SUFFIX}`;
      const transcriptBeforeHide = await sendAndWait(
        ctx,
        edited.id,
        `Reply with exactly ${hideMarker}`,
        hideMarker,
      );
      const hiddenSpec = automationSpec(
        hiddenRoutineName,
        `Reply with exactly ${hideMarker}`,
        true,
      );
      const hiddenCreated = expectOk(await call(ctx, "createAgentAutomation", {
        id: edited.id,
        spec: hiddenSpec,
      }), "createAgentAutomation for hide check");
      const hiddenRoutine = findByName(hiddenCreated, hiddenRoutineName) ??
        await findNamedRecord(ctx, "getAgentAutomations", edited.id, hiddenRoutineName);
      const hiddenRoutineId = extractId(hiddenRoutine);
      if (!hiddenRoutineId || hiddenRoutine?.isEnabled !== true) {
        throw new Error("hide-check routine was not created enabled");
      }
      ctx.createdAutomations.push({ agentId: edited.id, automationId: hiddenRoutineId });

      expectOk(await call(ctx, "setAgentHiddenFromSidebar", {
        id: edited.id,
        isHidden: true,
      }), "setAgentHiddenFromSidebar true");
      const hiddenListed = (await listAgents(ctx)).find((item) => item.id === edited.id);
      if (hiddenListed?.isHiddenFromSidebar !== true) {
        throw new Error("hidden Bot did not remain listed with its hidden flag");
      }
      const transcriptWhileHidden = expectOk(await call(ctx, "getAgentTranscript", {
        id: edited.id,
      }), "getAgentTranscript while hidden");
      if (!JSON.stringify(transcriptWhileHidden).includes(hideMarker) ||
          listBody(transcriptWhileHidden).length === 0) {
        throw new Error("hidden Bot transcript did not remain available");
      }
      const routinesWhileHidden = await getAutomations(ctx, edited.id);
      const stillEnabled = routinesWhileHidden.find((item) => item.id === hiddenRoutineId);
      if (stillEnabled?.isEnabled !== true) {
        throw new Error("hidden Bot routine did not remain enabled");
      }
      expectOk(await call(ctx, "setAgentHiddenFromSidebar", {
        id: edited.id,
        isHidden: false,
      }), "setAgentHiddenFromSidebar false");
      const unhiddenListed = (await listAgents(ctx)).find((item) => item.id === edited.id);
      if (unhiddenListed?.isHiddenFromSidebar !== false) {
        throw new Error("Bot remained hidden after unhide");
      }

      const settingsResponse = await call(ctx, "getHostSettings", {}, { record: false });
      const settings = expectOk(settingsResponse, "getHostSettings");
      const settingsBefore = hostSettingsProjection(settings);
      ctx.sidebarSettingsBaseline = settingsBefore;
      addProjectedCall(ctx, settingsResponse, settingsBefore);
      const testSectionId = `r2-${RUN_SUFFIX}`;
      const testSection = {
        id: testSectionId,
        name: `R2 ${RUN_SUFFIX}`,
        agentIds: [edited.id],
      };
      let pinRoundTrip = false;
      let sectionRoundTrip = false;
      try {
        const pinned = [...new Set([...settingsBefore.pinnedAgentIds, edited.id])];
        const setPinned = await call(ctx, "setHostSettings", {
          pinnedAgentIds: pinned,
        }, { record: false });
        const pinnedResult = expectOk(setPinned, "setHostSettings pinnedAgentIds");
        const pinnedProjection = hostSettingsProjection(pinnedResult);
        addProjectedCall(ctx, setPinned, pinnedProjection);
        const pinnedReadback = await call(ctx, "getHostSettings", {}, { record: false });
        const pinnedSettings = hostSettingsProjection(expectOk(pinnedReadback, "getHostSettings pinned read-back"));
        addProjectedCall(ctx, pinnedReadback, pinnedSettings);
        pinRoundTrip = pinnedSettings.pinnedAgentIds.includes(edited.id);
        if (!pinRoundTrip) throw new Error("pinnedAgentIds did not round-trip");

        const sections = [
          ...settingsBefore.sidebarSections.filter((section) => section?.id !== testSectionId),
          testSection,
        ];
        const setSections = await call(ctx, "setHostSettings", {
          sidebarSections: sections,
        }, { record: false });
        const sectionResult = expectOk(setSections, "setHostSettings sidebarSections");
        addProjectedCall(ctx, setSections, hostSettingsProjection(sectionResult));
        const sectionsReadback = await call(ctx, "getHostSettings", {}, { record: false });
        const sectionSettings = hostSettingsProjection(expectOk(sectionsReadback, "getHostSettings sections read-back"));
        addProjectedCall(ctx, sectionsReadback, sectionSettings);
        const readSection = sectionSettings.sidebarSections.find((section) => section?.id === testSectionId);
        sectionRoundTrip = readSection?.name === testSection.name &&
          Array.isArray(readSection?.agentIds) &&
          readSection.agentIds.includes(edited.id);
        if (!sectionRoundTrip) throw new Error("sidebarSections did not round-trip");
      } finally {
        const restore = await call(ctx, "setHostSettings", {
          pinnedAgentIds: settingsBefore.pinnedAgentIds,
          sidebarSections: settingsBefore.sidebarSections,
        }, { record: false });
        const restored = expectOk(restore, "restore host sidebar settings");
        addProjectedCall(ctx, restore, hostSettingsProjection(restored));
        const restoreReadback = await call(ctx, "getHostSettings", {}, { record: false });
        const restoredSettings = hostSettingsProjection(expectOk(restoreReadback, "getHostSettings restored read-back"));
        addProjectedCall(ctx, restoreReadback, restoredSettings);
        if (!isDeepStrictEqual(restoredSettings, settingsBefore)) {
          throw new Error("pinned/sidebar settings did not restore to their pre-test values");
        }
        ctx.sidebarSettingsRestored = true;
      }

      appendNotCovered(
        ctx,
        "dedicated sidebar-group API",
        "Searched source/host/gateway-protocol.ts, source/host/host-gateway-api.ts, and source/host/extensions/settings/settings-service.ts for sidebarGroup/createSidebarGroup/group-sidebar; found createGroup/setGroupMembers for chat groups and pinnedAgentIds/sidebarSections in host settings, but no separate sidebar-group command.",
        "Pinned-agent and sidebar-section settings were round-tripped. A dedicated sidebar-group API was not found; chat group methods do not represent sidebar sections.",
      );

      const routineDelete = await call(ctx, "deleteAgentAutomation", {
        id: edited.id,
        automationId: hiddenRoutineId,
      });
      expectOk(routineDelete, "deleteAgentAutomation hide-check cleanup");
      const hiddenRoutineAfterDelete = await getAutomations(ctx, edited.id);
      if (hiddenRoutineAfterDelete.some((item) => item.id === hiddenRoutineId)) {
        throw new Error("hide-check routine remained after cleanup");
      }
      ctx.createdAutomations = ctx.createdAutomations.filter(
        (item) => item.automationId !== hiddenRoutineId,
      );

      const deletedIds = [];
      for (const item of created) {
        const deletion = await call(ctx, "deleteAgent", { id: item.id });
        expectOk(deletion, "deleteAgent cap cleanup");
        ctx.deletedAgents.add(item.id);
        deletedIds.push(item.id);
      }
      const afterDelete = await listAgents(ctx);
      if (agentInventoryCount(afterDelete) !== initialCount ||
          created.some((item) => afterDelete.some((agent) => agent.id === item.id))) {
        throw new Error(`agent count after cap cleanup was ${agentInventoryCount(afterDelete)}, expected ${initialCount}`);
      }
      const dfAfter = guestExec(ctx, "df -B1 --output=avail /", 60);
      if (dfAfter.exitCode !== 0) throw new Error("guest df after cap cleanup failed");
      const freeBytesAfter = parseAvailableBytes(dfAfter.output);
      if (freeBytesAfter == null) throw new Error("guest df after cap cleanup returned no available bytes");
      ctx.guestDfAfterCap = { availableBytes: freeBytesAfter };

      return {
        initialOwnedAgents: initialCount,
        createdCount: created.length,
        countAtCap: agentInventoryCount(atCap),
        overflow: {
          status: overflow.status,
          body: overflow.body,
          rejected: overflowRejected,
          mentionsFiftyLimit: true,
        },
        edited: {
          id: edited.id,
          nameConfirmed: true,
          descriptionConfirmed: true,
        },
        hiddenBot: {
          listedHidden: true,
          transcriptEntryCountBefore: listBody(transcriptBeforeHide).length,
          transcriptEntryCountWhileHidden: listBody(transcriptWhileHidden).length,
          enabledRoutineRetained: true,
          unhidden: true,
        },
        sidebar: { pinRoundTrip, sectionRoundTrip, restored: true },
        deletedCapBotCount: deletedIds.length,
        countAfterCleanup: agentInventoryCount(afterDelete),
        guestDfBefore: freeBytesBefore,
        guestDfAfterCapCleanup: freeBytesAfter,
      };
    },
  },
  {
    id: "routine-cap-50",
    run: async (ctx) => {
      requireReady(ctx);
      const agent = await createAgent(
        ctx,
        `RoutineCap-${RUN_SUFFIX}`,
        "R2 routine capacity test",
      );
      const createdIds = [];
      for (let index = 0; index < AUTOMATION_LIMIT; index += 1) {
        const name = `Routine-${RUN_SUFFIX}-${index + 1}`;
        const response = await call(ctx, "createAgentAutomation", {
          id: agent.id,
          spec: automationSpec(name, "Disabled r2 cap test routine", false),
        }, { record: false });
        if (response.status !== 200) {
          addProjectedCall(ctx, response, {
            createdCount: createdIds.length,
            error: response.error ?? null,
          });
          throw new Error(`routine ${index + 1} createAgentAutomation returned HTTP ${response.status}, expected 200`);
        }
        const records = listBody(response.body);
        const created = findByName(response.body, name);
        const automationId = extractId(created);
        if (!created || !automationId || created.isEnabled !== false) {
          addProjectedCall(ctx, response, {
            createdCount: records.length,
            foundRequestedRoutine: Boolean(created),
            isEnabled: created?.isEnabled ?? null,
          });
          throw new Error(`routine ${index + 1} was not created disabled`);
        }
        createdIds.push(automationId);
        ctx.createdAutomations.push({ agentId: agent.id, automationId });
        addProjectedCall(ctx, response, {
          createdCount: records.length,
          createdId: automationId,
          createdName: name,
          isEnabled: false,
        });
      }

      const overflowName = `Routine-${RUN_SUFFIX}-overflow`;
      const overflow = await call(ctx, "createAgentAutomation", {
        id: agent.id,
        spec: automationSpec(overflowName, "51st disabled r2 cap test routine", false),
      });
      const afterOverflow = await getAutomations(ctx, agent.id);
      const overflowCreated = afterOverflow.some((item) => item.name === overflowName);
      if (afterOverflow.length !== AUTOMATION_LIMIT || overflowCreated) {
        throw new Error(`51st routine attempt left ${afterOverflow.length} routines or created the overflow routine`);
      }

      const deletionStatuses = [];
      for (const automationId of createdIds) {
        const deletion = await call(ctx, "deleteAgentAutomation", {
          id: agent.id,
          automationId,
        }, { record: false });
        deletionStatuses.push(deletion.status);
        expectOk(deletion, "deleteAgentAutomation routine-cap cleanup");
        ctx.createdAutomations = ctx.createdAutomations.filter(
          (item) => item.automationId !== automationId,
        );
      }
      const afterDelete = await getAutomations(ctx, agent.id);
      if (afterDelete.length !== 0) {
        throw new Error(`routine-cap cleanup left ${afterDelete.length} routines`);
      }
      return {
        createdCount: createdIds.length,
        allCreatesReturnedHttp200: true,
        allDisabled: true,
        overflowAttempt: {
          status: overflow.status,
          body: overflow.body,
          created: overflowCreated,
          listedAfterAttempt: afterOverflow.length,
        },
        deletionCount: deletionStatuses.length,
        deletionStatuses,
        remainingAfterDelete: afterDelete.length,
      };
    },
  },
  {
    id: "routine-run-history-20",
    run: async (ctx) => {
      requireReady(ctx);
      const agent = await createAgent(
        ctx,
        `RoutineHistory-${RUN_SUFFIX}`,
        "R2 automation run-history test",
      );
      const name = `History-${RUN_SUFFIX}`;
      const spec = automationSpec(name, "Reply with exactly RUNOK", false);
      const created = expectOk(await call(ctx, "createAgentAutomation", {
        id: agent.id,
        spec,
      }), "createAgentAutomation run-history");
      const automation = findByName(created, name) ??
        await findNamedRecord(ctx, "getAgentAutomations", agent.id, name);
      const automationId = extractId(automation);
      if (!automationId) throw new Error("run-history automation had no id");
      ctx.createdAutomations.push({ agentId: agent.id, automationId });

      const completedRuns = [];
      for (let index = 0; index < 22; index += 1) {
        const before = automationById(await getAutomations(ctx, agent.id, { record: false }), automationId);
        const priorId = before?.runs?.[0]?.id;
        expectOk(await call(ctx, "runAgentAutomationNow", {
          id: agent.id,
          automationId,
        }), "runAgentAutomationNow");
        const result = await waitFor(
          ctx,
          "getAgentAutomations",
          { id: agent.id },
          (body) => {
            const current = automationById(listBody(body), automationId);
            const newest = current?.runs?.[0];
            return newest?.id != null &&
              newest.id !== priorId &&
              newest.status !== "running";
          },
          { timeoutMs: TIMEOUT_MS, intervalMs: POLL_MS },
        );
        const latestAutomation = automationById(listBody(result), automationId);
        const latestRun = latestAutomation?.runs?.[0];
        if (!latestRun?.id || latestRun.status === "running") {
          throw new Error(`manual run ${index + 1} did not finish`);
        }
        if (latestRun.status !== "ok") {
          throw new Error(`manual run ${index + 1} ended with status ${latestRun.status}`);
        }
        completedRuns.push({
          id: latestRun.id,
          status: latestRun.status,
          startedAt: latestRun.startedAt,
          finishedAt: latestRun.finishedAt,
        });
      }

      const after22 = automationById(await getAutomations(ctx, agent.id), automationId);
      if (!Array.isArray(after22?.runs) || after22.runs.length !== 20) {
        throw new Error(`run history retained ${after22?.runs?.length ?? 0} runs, expected 20`);
      }
      const firstTwoIds = completedRuns.slice(0, 2).map((run) => run.id);
      const retainedIds = new Set(after22.runs.map((run) => run.id));
      if (firstTwoIds.some((id) => retainedIds.has(id))) {
        throw new Error("one of the first two run IDs remained in the 20-run history");
      }

      expectOk(await call(ctx, "setAgentAutomationEnabled", {
        id: agent.id,
        automationId,
        isEnabled: false,
      }), "setAgentAutomationEnabled false");
      let current = automationById(await getAutomations(ctx, agent.id), automationId);
      if (current?.isEnabled !== false) throw new Error("routine pause did not read back isEnabled=false");
      expectOk(await call(ctx, "setAgentAutomationEnabled", {
        id: agent.id,
        automationId,
        isEnabled: true,
      }), "setAgentAutomationEnabled true");
      current = automationById(await getAutomations(ctx, agent.id), automationId);
      if (current?.isEnabled !== true) throw new Error("routine resume did not read back isEnabled=true");

      const editedPrompt = "Reply with exactly RUNOK-EDITED";
      const editedSpec = {
        name: current.name,
        prompt: editedPrompt,
        trigger: current.trigger,
        isEnabled: current.isEnabled,
      };
      expectOk(await call(ctx, "updateAgentAutomation", {
        id: agent.id,
        automationId,
        spec: editedSpec,
      }), "updateAgentAutomation");
      current = automationById(await getAutomations(ctx, agent.id), automationId);
      if (current?.prompt !== editedPrompt) {
        throw new Error("edited routine prompt did not read back");
      }

      expectOk(await call(ctx, "setAgentAutomationEnabled", {
        id: agent.id,
        automationId,
        isEnabled: false,
      }), "setAgentAutomationEnabled false before delete");
      current = automationById(await getAutomations(ctx, agent.id), automationId);
      if (current?.isEnabled !== false) {
        throw new Error("routine was not paused before deletion");
      }
      const transcriptBeforeDelete = expectOk(await call(ctx, "getAgentTranscript", {
        id: agent.id,
      }), "getAgentTranscript before routine delete");
      const runTurnsBeforeDelete = countNamedTranscriptTurns(transcriptBeforeDelete, name);
      const assistantMessageIdsBeforeDelete = new Set(
        assistantSendMessageIds(transcriptBeforeDelete),
      );
      const assistantRunokBeforeDelete = countExactAssistantText(
        transcriptBeforeDelete,
        "RUNOK",
      );
      const assistantRunokEditedBeforeDelete = countExactAssistantText(
        transcriptBeforeDelete,
        "RUNOK-EDITED",
      );
      expectOk(await call(ctx, "deleteAgentAutomation", {
        id: agent.id,
        automationId,
      }), "deleteAgentAutomation run-history cleanup");
      const absent = await getAutomations(ctx, agent.id);
      if (absent.some((item) => item.id === automationId || item.name === name)) {
        throw new Error("deleted routine remained in getAgentAutomations");
      }
      ctx.createdAutomations = ctx.createdAutomations.filter(
        (item) => item.automationId !== automationId,
      );

      const quietStartedAtMs = Date.now();
      let monitorPolls = 0;
      while (Date.now() - quietStartedAtMs < 70_000) {
        const transcript = await call(ctx, "getAgentTranscript", { id: agent.id }, { record: false });
        expectOk(transcript, "getAgentTranscript routine-deletion quiet window");
        const newTurnCount = countNamedTranscriptTurns(transcript.body, name);
        const assistantRunokCount = countExactAssistantText(transcript.body, "RUNOK");
        const assistantRunokEditedCount = countExactAssistantText(
          transcript.body,
          "RUNOK-EDITED",
        );
        const newAssistantMessageIds = assistantSendMessageIds(transcript.body)
          .filter((id) => !assistantMessageIdsBeforeDelete.has(id));
        if (newTurnCount > runTurnsBeforeDelete ||
            assistantRunokCount > assistantRunokBeforeDelete ||
            assistantRunokEditedCount > assistantRunokEditedBeforeDelete ||
            newAssistantMessageIds.length > 0) {
          ctx.caseRecord.calls.push(transcript.entry);
          throw new Error(
            `a routine turn started during the 70-second post-delete window (new assistant messages: ${newAssistantMessageIds.length})`,
          );
        }
        const listed = await call(ctx, "getAgentAutomations", { id: agent.id }, { record: false });
        expectOk(listed, "getAgentAutomations routine-deletion quiet window");
        if (listBody(listed.body).some((item) => item.id === automationId || item.name === name)) {
          throw new Error("deleted routine reappeared during the 70-second quiet window");
        }
        monitorPolls += 1;
        await new Promise((resolvePromise) => setTimeout(resolvePromise, POLL_MS));
      }
      const quietEndedAtMs = Date.now();
      const transcriptAfterQuiet = expectOk(await call(ctx, "getAgentTranscript", {
        id: agent.id,
      }), "getAgentTranscript after routine-deletion quiet window");
      const runTurnsAfterDelete = countNamedTranscriptTurns(transcriptAfterQuiet, name);
      const assistantRunokAfterDelete = countExactAssistantText(transcriptAfterQuiet, "RUNOK");
      const assistantRunokEditedAfterQuiet = countExactAssistantText(
        transcriptAfterQuiet,
        "RUNOK-EDITED",
      );
      const assistantMessageIdsAfterQuiet = assistantSendMessageIds(transcriptAfterQuiet);
      const newAssistantMessageIdsAfterQuiet = assistantMessageIdsAfterQuiet
        .filter((id) => !assistantMessageIdsBeforeDelete.has(id));
      const finalAutomations = await getAutomations(ctx, agent.id);
      if (runTurnsAfterDelete !== runTurnsBeforeDelete ||
          assistantRunokAfterDelete !== assistantRunokBeforeDelete ||
          assistantRunokEditedAfterQuiet !== assistantRunokEditedBeforeDelete ||
          newAssistantMessageIdsAfterQuiet.length > 0 ||
          finalAutomations.some((item) => item.id === automationId || item.name === name)) {
        throw new Error("routine run or record appeared during the post-delete quiet window");
      }

      return {
        automationId,
        totalManualRuns: completedRuns.length,
        completedRuns,
        firstTwoRunIds: firstTwoIds,
        retainedRunCount: after22.runs.length,
        firstTwoAbsent: true,
        retainedRunStatuses: after22.runs.map((run) => ({
          id: run.id,
          status: run.status,
        })),
        pauseReadBack: true,
        resumeReadBack: true,
        editReadBack: true,
        deletedAndAbsent: true,
        assistantRunokRepliesBeforeDelete: assistantRunokBeforeDelete,
        assistantRunokRepliesAfterQuietWindow: assistantRunokAfterDelete,
        assistantRunokEditedRepliesBeforeDelete: assistantRunokEditedBeforeDelete,
        assistantRunokEditedRepliesAfterQuietWindow: assistantRunokEditedAfterQuiet,
        assistantMessagesBeforeDelete: assistantMessageIdsBeforeDelete.size,
        assistantMessagesAfterQuietWindow: assistantMessageIdsAfterQuiet.length,
        newAssistantMessagesAfterDelete: newAssistantMessageIdsAfterQuiet.length,
        quietWindowMs: quietEndedAtMs - quietStartedAtMs,
        quietWindowPolls: monitorPolls,
        newRoutineTurns: runTurnsAfterDelete - runTurnsBeforeDelete,
        newRunokReplies: assistantRunokAfterDelete - assistantRunokBeforeDelete,
      };
    },
  },
  {
    id: "delete-state",
    run: async (ctx) => {
      requireReady(ctx);
      const agent = await createAgent(
        ctx,
        `DeleteProbe-${RUN_SUFFIX}`,
        "R2 deletion-state probe",
      );
      const marker = `DELETEOK-${RUN_SUFFIX}`;
      const transcriptBefore = await sendAndWait(
        ctx,
        agent.id,
        `Reply with exactly ${marker}`,
        marker,
      );
      const routineName = `DeleteRoutine-${RUN_SUFFIX}`;
      const routineResponse = expectOk(await call(ctx, "createAgentAutomation", {
        id: agent.id,
        spec: automationSpec(routineName, `Reply with exactly ${marker}`, false),
      }), "createAgentAutomation delete-state");
      const routine = findByName(routineResponse, routineName) ??
        await findNamedRecord(ctx, "getAgentAutomations", agent.id, routineName);
      const automationId = extractId(routine);
      if (!automationId || routine?.isEnabled !== false) {
        throw new Error("delete-state disabled routine was not created");
      }
      ctx.createdAutomations.push({ agentId: agent.id, automationId });

      appendNotCovered(
        ctx,
        "save a memory before deletion",
        "Searched source/host/gateway-protocol.ts and source/host/host-gateway-api.ts for saveAgentMemory/writeAgentMemory/addAgentMemory/setAgentMemory; the exposed memory commands are getAgentMemories, deleteAgentMemory, and clearAgentMemories.",
        "No supported gateway memory-save API was found; no memory write was attempted.",
      );
      appendNotCovered(
        ctx,
        "read deleted profile through a profile API",
        "Searched source/host/gateway-protocol.ts and source/host/host-gateway-api.ts for getAgentProfile/getAgentProfileText; getAgentProfileText is internal to TranscriptManager and is not in the gateway method table.",
        "No supported gateway profile-read command was found. listAgents absence and guest agent-directory deletion are still checked.",
      );

      const findPath = guestExec(
        ctx,
        `find /home -type d -path '*/agents/${agent.id}' -print -quit`,
        90,
      );
      if (findPath.exitCode !== 0) throw new Error("could not inspect the source-derived agent directory");
      const agentDir = findPath.output.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).at(-1);
      if (!agentDir ||
          !agentDir.startsWith("/home/") ||
          !agentDir.endsWith(`/agents/${agent.id}`)) {
        throw new Error("source-derived resolveSandAgentDir path was not found on the guest");
      }
      const dirCheck = guestExec(
        ctx,
        `test -d ${shellQuote(agentDir)} && printf 'exists\\n'`,
        60,
      );
      if (dirCheck.exitCode !== 0 || !dirCheck.output.includes("exists")) {
        throw new Error("agent directory did not exist before deleteAgent");
      }

      const probePath = `/workspace/delete-probe-${RUN_SUFFIX}.txt`;
      ctx.temporaryGuestFiles.push(probePath);
      const createProbe = guestExec(
        ctx,
        `printf 'r2-delete-state-probe\\n' > ${shellQuote(probePath)} && test -f ${shellQuote(probePath)} && printf 'exists\\n'`,
        60,
      );
      if (createProbe.exitCode !== 0 || !createProbe.output.includes("exists")) {
        throw new Error("shared workspace deletion probe could not be created");
      }

      expectOk(await call(ctx, "deleteAgent", { id: agent.id }), "deleteAgent delete-state");
      const afterList = await listAgents(ctx);
      const absentFromList = !afterList.some((item) => item.id === agent.id);
      if (!absentFromList) throw new Error("deleted Bot remained in listAgents");
      ctx.deletedAgents.add(agent.id);
      ctx.createdAutomations = ctx.createdAutomations.filter(
        (item) => item.automationId !== automationId,
      );

      const transcriptAfter = await call(ctx, "getAgentTranscript", { id: agent.id });
      const transcriptEmpty = isNotFoundOrEmpty(transcriptAfter);
      const automationsAfter = await call(ctx, "getAgentAutomations", { id: agent.id });
      const automationsEmpty = isNotFoundOrEmpty(automationsAfter);
      const memoriesAfter = await call(ctx, "getAgentMemories", { id: agent.id });
      const memoriesEmpty = isNotFoundOrEmpty(memoriesAfter);
      if (!transcriptEmpty || !automationsEmpty || !memoriesEmpty) {
        throw new Error("a deleted Bot transcript, automation list, or memory list was not empty/not-found");
      }

      const dirAfter = guestExec(
        ctx,
        `if [ -e ${shellQuote(agentDir)} ]; then printf 'present\\n'; else printf 'gone\\n'; fi`,
        60,
      );
      if (dirAfter.exitCode !== 0 || !dirAfter.output.includes("gone")) {
        throw new Error("agent directory remained after deleteAgent");
      }
      const probeAfter = guestExec(
        ctx,
        `test -f ${shellQuote(probePath)} && printf 'exists\\n'`,
        60,
      );
      if (probeAfter.exitCode !== 0 || !probeAfter.output.includes("exists")) {
        throw new Error("shared workspace probe file did not remain after deleting the Bot");
      }
      const removeProbe = guestExec(
        ctx,
        `rm -f -- ${shellQuote(probePath)} && if [ -e ${shellQuote(probePath)} ]; then printf 'present\\n'; else printf 'removed\\n'; fi`,
        60,
      );
      if (removeProbe.exitCode !== 0 || !removeProbe.output.includes("removed")) {
        throw new Error("shared workspace probe file could not be removed after verification");
      }
      ctx.temporaryGuestFiles = ctx.temporaryGuestFiles.filter(
        (path) => path !== probePath,
      );

      return {
        id: agent.id,
        markerReplySeen: JSON.stringify(transcriptBefore).includes(marker),
        disabledRoutineCreated: true,
        sourceDerivedAgentDir: agentDir,
        agentDirSource: {
          files: [
            "source/host/storage/agent-paths.ts",
            "source/host/host-paths.ts",
          ],
          resolution: "resolveSandAgentDir(id) joins getSandRootDir()/agents/<id>; the guest search matched that exact suffix under /home because the source permits configurable data roots.",
        },
        agentDirExistedBeforeDelete: true,
        listAgentsAbsent: absentFromList,
        transcriptAfterDelete: {
          status: transcriptAfter.status,
          emptyOrNotFound: transcriptEmpty,
          body: transcriptAfter.body,
        },
        automationsAfterDelete: {
          status: automationsAfter.status,
          emptyOrNotFound: automationsEmpty,
          body: automationsAfter.body,
        },
        memoriesAfterDelete: {
          status: memoriesAfter.status,
          emptyOrNotFound: memoriesEmpty,
          body: memoriesAfter.body,
        },
        agentDirGoneAfterDelete: true,
        sharedWorkspaceProbeRemained: true,
        sharedWorkspaceProbeRemoved: true,
      };
    },
  },
  {
    id: "secret-masking",
    run: async (ctx) => {
      requireReady(ctx);
      const statusResponse = await call(ctx, "getBoxSecretsStatus", {}, { record: false });
      const status = expectOk(statusResponse, "getBoxSecretsStatus");
      const keys = Array.isArray(status?.keys) ? status.keys : [];
      addProjectedCall(ctx, statusResponse, {
        keyCount: keys.length,
        hasR2Probe: keys.includes("R2_PROBE"),
        isApplied: status?.isApplied ?? null,
        lastAppliedAtMs: status?.lastAppliedAtMs ?? null,
      });

      appendNotCovered(
        ctx,
        "save/list Bot secrets with name, description, and masked value",
        "Searched source/host/gateway-protocol.ts and source/host/host-gateway-api.ts for listSecrets/upsertSecrets/removeSecrets; searched source/electron-main/secrets/secrets-ipc.ts and source/shared/rpc/main.ts for their IPC contracts.",
        "The gateway exposes only setBoxSecrets({secrets: Record<string,string>}) and getBoxSecretsStatus() (keys, applied state, timestamp). listSecrets/upsertSecrets/removeSecrets exist only in local Electron IPC; listSecrets returns keys and persistence state, upsertSecrets accepts key/value entries without descriptions. No supported gateway route for those IPC methods was found.",
      );
      appendNotCovered(
        ctx,
        "inject R2_PROBE and scan transcript, guest files/logs, host logs, and evidence",
        "Searched source/host/extensions/secrets/secrets-service.ts and source/host/host-gateway-api.ts for box-secret mutation semantics.",
        "setBoxSecrets replaces the complete desired secret map and applies it with replace=true. The existing secret values cannot be read back through the gateway, so adding only R2_PROBE could remove existing credentials. No secret was written, no sentinel was generated, and no environment/log scan was claimed.",
      );

      return {
        caseStatus: "BLOCKED",
        subchecks: [
          { name: "Bot secret save/list/description API", status: "NOT_COVERED" },
          { name: "R2_PROBE environment injection and masking scans", status: "NOT_COVERED" },
        ],
        boxSecretStatus: {
          status: statusResponse.status,
          keyCount: keys.length,
          hasR2Probe: keys.includes("R2_PROBE"),
          isApplied: status?.isApplied ?? null,
        },
        sentinelGenerated: false,
        writesPerformed: false,
      };
    },
  },
  {
    id: "search-cross-bot",
    run: async (ctx) => {
      requireReady(ctx);
      const marker = `r2marker${RUN_SUFFIX.replaceAll("-", "")}`;
      const a = await createAgent(ctx, `SearchA-${RUN_SUFFIX}`, "R2 cross-Bot search A");
      const b = await createAgent(ctx, `SearchB-${RUN_SUFFIX}`, "R2 cross-Bot search B");
      const link = `https://example.com/${marker}`;
      const messagePrompt = `Keep this marker in the conversation: ${marker}. Link: ${link}. Reply with exactly SEARCHOK.`;
      const transcript = await sendAndWait(ctx, a.id, messagePrompt, "SEARCHOK");

      const markerWordResults = listBody(expectOk(await call(ctx, "searchAgents", {
        query: marker,
        limit: 20,
      }), "searchAgents marker query"));
      const messageHit = markerWordResults.some((item) =>
        item.agentId === a.id &&
        item.role === "user" &&
        String(item.snippet ?? "").includes(marker));
      if (!messageHit) throw new Error("searchAgents did not return Bot A's marker message");

      const linkMarkerResults = listBody(expectOk(await call(ctx, "searchAgents", {
        query: "example.com",
        limit: 20,
      }), "searchAgents link query"));
      const linkHit = linkMarkerResults.some((item) =>
        item.agentId === a.id &&
        item.role === "user" &&
        String(item.snippet ?? "").includes(link));
      if (!linkHit) throw new Error("searchAgents did not return Bot A's marker link");

      const fileName = `${marker}.txt`;
      const fileContent = `R2 search attachment ${marker}\n`;
      const upload = expectOk(await call(ctx, "uploadAttachment", {
        filename: fileName,
        bytesBase64: Buffer.from(fileContent, "utf8").toString("base64"),
        agentId: a.id,
      }), "uploadAttachment search file");
      const filePath = upload?.path;
      if (typeof filePath !== "string" || filePath.length === 0) {
        throw new Error("uploadAttachment returned no path for the search marker file");
      }
      const fileMarker = `FILEOK-${RUN_SUFFIX}`;
      await sendAndWait(ctx, a.id, `Read the attached file and reply with exactly ${fileMarker}`, fileMarker, {
        attachmentPaths: [filePath],
        attachmentNames: [fileName],
      });
      const routineName = `Routine-${marker}`;
      const routineCreated = expectOk(await call(ctx, "createAgentAutomation", {
        id: b.id,
        spec: automationSpec(routineName, "Disabled cross-Bot routine search marker", false),
      }), "createAgentAutomation search marker");
      const routine = findByName(routineCreated, routineName) ??
        await findNamedRecord(ctx, "getAgentAutomations", b.id, routineName);
      const routineId = extractId(routine);
      if (!routineId) throw new Error("marker routine on Bot B was not created");
      ctx.createdAutomations.push({ agentId: b.id, automationId: routineId });

      appendNotCovered(
        ctx,
        "search by routine name across Bots",
        "Searched source/host/gateway-protocol.ts, source/host/host-gateway-api.ts, source/host/extensions/transcript/roster-search.ts, and source/host/extensions/content-search/search-index-db.ts for routine/automation search commands and indexes.",
        "searchAgents indexes transcript message bodies; searchMedia indexes attachment file_name. No search API or index for automation names/routine definitions was found. The marker routine is confirmed by getAgentAutomations on Bot B, but not by search.",
      );
      appendNotCovered(
        ctx,
        "search file contents rather than attachment names",
        "Searched source/host/extensions/content-search/search-index-db.ts for media_fts and source/host/extensions/content-search/search-index-writer.ts for indexed media fields.",
        "The media FTS table indexes file_name only. The metadata filename query is exercised separately; searching arbitrary file contents is NOT_COVERED.",
      );

      let mediaResults = [];
      let mediaSearchError;
      try {
        mediaResults = listBody(await waitFor(
          ctx,
          "searchMedia",
          { query: marker, limit: 20 },
          (body) => listBody(body).some((item) =>
            item.agentId === a.id && String(item.fileName ?? "").includes(marker)),
          { timeoutMs: TIMEOUT_MS, intervalMs: POLL_MS },
        ));
      } catch (error) {
        mediaSearchError = sanitizeString(error?.message ?? error, ctx.literals);
      }
      const fileNameHit = mediaResults.some((item) =>
        item.agentId === a.id &&
        String(item.fileName ?? "").includes(marker));

      return {
        caseStatus: fileNameHit ? "PASS" : "FAIL",
        marker,
        botA: a.id,
        botB: b.id,
        transcriptEntryCount: listBody(transcript).length,
        messageSearch: { query: marker, hit: messageHit },
        linkSearch: { query: "example.com", hit: linkHit, link },
        fileNameSearch: {
          query: marker,
          hit: fileNameHit,
          fileName,
          ...(mediaSearchError ? { error: mediaSearchError } : {}),
          fileContentSearch: "NOT_COVERED",
        },
        routine: {
          id: routineId,
          listedOnBotB: true,
          searchable: "NOT_COVERED",
        },
        subchecks: [
          { name: "cross-Bot message search", status: "PASS" },
          { name: "link in message search", status: "PASS" },
          {
            name: "marker attachment filename search",
            status: fileNameHit ? "PASS" : "FAIL",
            resultCount: mediaResults.length,
          },
          { name: "attachment file-content search", status: "NOT_COVERED" },
          { name: "routine-name search", status: "NOT_COVERED" },
        ],
      };
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
    const result = await definition.run(ctx);
    caseRecord.result = result;
    caseRecord.status = result?.caseStatus ?? "PASS";
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
  const results = [];
  const runCleanup = async (command, body) => {
    const startedAtMs = Date.now();
    const response = await call(ctx, command, body, { record: false });
    results.push({
      command,
      request: { body: sanitize(body, ctx.literals) },
      response: sanitize({
        status: response.status,
        itemCount: listBody(response.body).length,
        error: response.error,
      }, ctx.literals),
      startedAtMs,
      endedAtMs: Date.now(),
      durationMs: Date.now() - startedAtMs,
    });
    return response;
  };
  if (ctx.sidebarSettingsBaseline && !ctx.sidebarSettingsRestored) {
    await runCleanup("setHostSettings", ctx.sidebarSettingsBaseline);
    const readback = await runCleanup("getHostSettings", {});
    const restored = readback.status >= 200 &&
      readback.status < 300 &&
      isDeepStrictEqual(
        hostSettingsProjection(readback.body),
        ctx.sidebarSettingsBaseline,
      );
    results.push({
      command: "verifyHostSidebarSettingsRestore",
      response: { status: readback.status, restored },
    });
    if (restored) ctx.sidebarSettingsRestored = true;
  }
  for (const item of [...ctx.createdAutomations].reverse()) {
    await runCleanup("deleteAgentAutomation", {
      id: item.agentId,
      automationId: item.automationId,
    });
  }
  for (const path of [...ctx.temporaryGuestFiles].reverse()) {
    const startedAtMs = Date.now();
    const result = guestExec(
      ctx,
      `rm -f -- ${shellQuote(path)} && if [ -e ${shellQuote(path)} ]; then printf 'present\\n'; else printf 'removed\\n'; fi`,
      60,
      { record: false },
    );
    results.push({
      command: "removeGuestWorkspaceProbe",
      request: { path },
      response: {
        exitCode: result.exitCode,
        output: sanitizeString(result.output, ctx.literals),
      },
      startedAtMs,
      endedAtMs: Date.now(),
      durationMs: Date.now() - startedAtMs,
    });
    if (result.exitCode === 0 && result.output.includes("removed")) {
      ctx.temporaryGuestFiles = ctx.temporaryGuestFiles.filter(
        (temporaryPath) => temporaryPath !== path,
      );
    }
  }
  for (const id of [...ctx.createdAgents.keys()].reverse()) {
    if (!ctx.deletedAgents.has(id)) {
      const response = await runCleanup("deleteAgent", { id });
      if (response.status >= 200 && response.status < 300) {
        ctx.deletedAgents.add(id);
      }
    }
  }
  return results;
}

await mkdir(OUT, { recursive: true });
const ctx = {
  literals: new Set(API_KEY ? [API_KEY] : []),
  setup: [],
  agents: {},
  createdAgents: new Map(),
  createdAutomations: [],
  deletedAgents: new Set(),
  temporaryGuestFiles: [],
  sidebarSettingsBaseline: null,
  sidebarSettingsRestored: false,
  caseRecord: null,
  gateway: null,
  blockedReason: null,
  guestDfBefore: null,
  guestDfAfterCap: null,
  guestDfAfterRun: null,
  boxCountBefore: null,
  boxCountAfter: null,
  agentInventoryBefore: [],
  agentInventoryAfter: [],
  agentInventoryDelta: null,
  agentInventoryBeforeCaptured: false,
};

try {
  const boxCountStartedAtMs = Date.now();
  ctx.boxCountBefore = await readOrgBoxInventory(ctx);
  const boxPreflightPassed = ctx.boxCountBefore.httpStatus === 200 &&
    ctx.boxCountBefore.taggedBoxCount === 1 &&
    ctx.boxCountBefore.boxes?.[0]?.sessionIdPrefix === EXPECTED_BOX_PREFIX &&
    ctx.boxCountBefore.boxes?.[0]?.status === "running";
  recordSetup(ctx, "org box inventory preflight", boxCountStartedAtMs, {
    status: boxPreflightPassed ? "pass" : "fail",
    inventory: ctx.boxCountBefore,
  });
  if (!boxPreflightPassed) {
    ctx.blockedReason = "preflight did not confirm exactly one running grok-bot-box session with the expected a8dce654 prefix";
  }
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
  const session = ctx.blockedReason
    ? undefined
    : await connectDevboxSession({ origin: ORIGIN, apiKey: API_KEY, step });
  if (!ctx.blockedReason && !session) {
    ctx.blockedReason = "desktop authentication/Connect session could not be established";
  } else if (session) {
    const box = await ensureDevboxSandbox(session.grokBot, step);
    if (!box) {
      ctx.blockedReason = "EnsureSandBox did not return a usable box session";
    } else if (!String(box.podId).startsWith(EXPECTED_BOX_PREFIX)) {
      ctx.blockedReason = `EnsureSandBox returned unexpected box ${String(box.podId).slice(0, 16)}; expected ${EXPECTED_BOX_PREFIX}`;
    } else {
      ctx.gateway = createDevboxGateway(box);
      if (ctx.gateway?.token) ctx.literals.add(ctx.gateway.token);
      const startedAtMs = Date.now();
      try {
        const response = await gatewayRequest(ctx.gateway, "GET", "/health");
        const body = parseText(await response.text());
        recordSetup(ctx, "gateway /health", startedAtMs, {
          status: response.status === 200 ? "pass" : "fail",
          httpStatus: response.status,
          body,
        });
      if (response.status !== 200) {
        ctx.blockedReason = `gateway health returned HTTP ${response.status}`;
      }
      } catch (error) {
        recordSetup(ctx, "gateway /health", startedAtMs, {
          status: "fail",
          error: sanitizeString(error?.message ?? error, ctx.literals),
        });
        ctx.blockedReason = "gateway health request failed";
      }
    }
  }
  if (ctx.gateway && !ctx.blockedReason) {
    ctx.caseRecord = { calls: [] };
    const inventoryResponse = await call(ctx, "listAgents", {}, { record: false });
    const inventory = listBody(expectOk(inventoryResponse, "pre-run listAgents"));
    ctx.agentInventoryBefore = safeAgentInventory(inventory);
    ctx.agentInventoryBeforeCaptured = true;
    recordSetup(ctx, "agent inventory before run", Date.now(), {
      status: "pass",
      agentCount: inventory.length,
      agents: ctx.agentInventoryBefore,
    });
    ctx.caseRecord = null;
  }
} catch (error) {
  ctx.blockedReason = sanitizeString(error?.message ?? error, ctx.literals);
}

const results = [];
try {
  for (const definition of cases) {
    results.push(await runCase(ctx, definition));
  }
} finally {
  if (ctx.gateway) {
    ctx.caseRecord = { calls: [] };
    ctx.cleanup = await cleanup(ctx);
    const finalDf = guestExec(ctx, "df -B1 --output=avail /", 60, { record: false });
    if (finalDf.exitCode === 0) {
      const availableBytes = parseAvailableBytes(finalDf.output);
      if (availableBytes != null) ctx.guestDfAfterRun = { availableBytes };
    }
    const beforeFinalInventory = ctx.caseRecord;
    ctx.caseRecord = { calls: [] };
    const finalInventoryResponse = await call(ctx, "listAgents", {}, { record: false });
    if (finalInventoryResponse.status >= 200 && finalInventoryResponse.status < 300) {
      const finalAgents = listBody(finalInventoryResponse.body);
      ctx.agentInventoryAfter = safeAgentInventory(finalAgents);
      const initialIds = new Set(ctx.agentInventoryBefore.map((agent) => agent.id));
      ctx.agentInventoryDelta = {
        added: ctx.agentInventoryAfter.filter((agent) => !initialIds.has(agent.id)),
        removed: ctx.agentInventoryBefore.filter(
          (agent) => !ctx.agentInventoryAfter.some((current) => current.id === agent.id),
        ),
        remainingRunCreated: ctx.agentInventoryAfter.filter((agent) =>
          ctx.createdAgents.has(agent.id)),
      };
    } else {
      ctx.agentInventoryDelta = {
        error: `final listAgents returned HTTP ${finalInventoryResponse.status}`,
      };
    }
    ctx.caseRecord = beforeFinalInventory;
    ctx.boxCountAfter = await readOrgBoxInventory(ctx);
  } else {
    ctx.cleanup = [];
    ctx.boxCountAfter = await readOrgBoxInventory(ctx);
  }
}

const counts = Object.fromEntries(["PASS", "FAIL", "BLOCKED"].map((status) => [
  status,
  results.filter((item) => item.status === status).length,
]));
const summary = {
  runSuffix: RUN_SUFFIX,
  expectedBoxPrefix: EXPECTED_BOX_PREFIX,
  guestSessionId: GUEST_SESSION_ID,
  setup: sanitize(ctx.setup, ctx.literals),
  setupBlockedReason: ctx.blockedReason,
  guestDisk: {
    beforeBotCapCase: ctx.guestDfBefore,
    afterBotCapCleanup: ctx.guestDfAfterCap,
    afterRunCleanup: ctx.guestDfAfterRun,
    minimumBeforeCapBytes: MIN_FREE_BYTES,
  },
  orgBoxCount: {
    before: ctx.boxCountBefore,
    after: ctx.boxCountAfter,
  },
  agentInventory: {
    before: ctx.agentInventoryBefore,
    after: ctx.agentInventoryAfter,
    delta: ctx.agentInventoryDelta,
    beforeCaptured: ctx.agentInventoryBeforeCaptured,
  },
  temporaryGuestFilesRemaining: ctx.temporaryGuestFiles,
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
await writeFile(resolve(OUT, "box-count.json"), JSON.stringify({
  operation: "read-only org box count",
  origin: CONTROL_PLANE_ORIGIN,
  method: "GET",
  path: `/v3/organizations/${ORGANIZATION_ID}/sessions`,
  orgId: ORGANIZATION_ID,
  totalSessionsBefore: ctx.boxCountBefore?.totalSessions ?? null,
  totalSessionsAfter: ctx.boxCountAfter?.totalSessions ?? null,
  boxTag: "grok-bot-box",
  taggedBoxCountBefore: ctx.boxCountBefore?.taggedBoxCount ?? null,
  taggedBoxCountAfter: ctx.boxCountAfter?.taggedBoxCount ?? null,
  runningBoxCountBefore: ctx.boxCountBefore?.runningBoxCount ?? null,
  runningBoxCountAfter: ctx.boxCountAfter?.runningBoxCount ?? null,
  before: ctx.boxCountBefore?.boxes ?? [],
  after: ctx.boxCountAfter?.boxes ?? [],
}, null, 2));
console.log(`Summary PASS=${counts.PASS} FAIL=${counts.FAIL} BLOCKED=${counts.BLOCKED}`);
const safetyFailed = ctx.boxCountAfter?.httpStatus !== 200 ||
  ctx.boxCountAfter?.taggedBoxCount !== 1 ||
  ctx.boxCountAfter?.boxes?.[0]?.sessionIdPrefix !== EXPECTED_BOX_PREFIX ||
  (ctx.agentInventoryDelta?.remainingRunCreated?.length ?? 0) > 0 ||
  Boolean(ctx.agentInventoryDelta?.error) ||
  ctx.temporaryGuestFiles.length > 0 ||
  Boolean(ctx.sidebarSettingsBaseline && !ctx.sidebarSettingsRestored) ||
  (ctx.agentInventoryBeforeCaptured &&
    ((ctx.agentInventoryDelta?.added?.length ?? 0) > 0 ||
      (ctx.agentInventoryDelta?.removed?.length ?? 0) > 0));
process.exitCode = counts.FAIL > 0 || safetyFailed ? 1 : 0;
