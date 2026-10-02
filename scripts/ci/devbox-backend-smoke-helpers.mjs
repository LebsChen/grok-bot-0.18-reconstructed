import { createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-node";
import { createHash, randomBytes, randomUUID } from "node:crypto";

import { DashboardService } from "../../source/packages/proto/generated/aiserver/v1/dashboard_connect.js";
import { GrokBotService } from "../../source/packages/proto/generated/aiserver/v1/grok_bot_connect.js";
import { AiService } from "../../source/packages/proto/generated/aiserver/v1/aiserver_connect.js";
import { InferenceService } from "../../source/packages/proto/generated/aiserver/v1/inference_connect.js";

const PROD_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB";

const b64url = (buf) => Buffer.from(buf).toString("base64url");

export async function connectDevboxSession({ origin, apiKey, step }) {
  const uuid = randomUUID();
  const verifier = b64url(randomBytes(32));
  const challenge = b64url(createHash("sha256").update(verifier).digest());

  async function http(method, path, { token, json, headers } = {}) {
    const resp = await fetch(`${origin}${path}`, {
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

  const approve = await step("loginDeepControl/approve", () =>
    http("POST", "/loginDeepControl/approve", {
      token: apiKey, json: { uuid, challenge },
    }), (r) => ({ httpStatus: r.status }));
  if (approve?.status !== 200) return null;

  const poll = await step("auth/poll", () =>
    http("GET", `/auth/poll?uuid=${uuid}&verifier=${verifier}`),
    (r) => ({ httpStatus: r.status, keys: Object.keys(r.body ?? {}) }));
  const accessToken = poll?.body?.accessToken;
  const refreshToken = poll?.body?.refreshToken;
  if (!accessToken || !refreshToken) return null;

  const refreshed = await step("oauth/token refresh", () =>
    http("POST", "/oauth/token", {
      json: {
        client_id: PROD_CLIENT_ID,
        grant_type: "refresh_token",
        refresh_token: refreshToken,
      },
    }),
    (r) => {
      if (r.status !== 200 || !r.body?.access_token) {
        throw new Error(`refresh failed (HTTP ${r.status})`);
      }
      return { httpStatus: r.status, keys: Object.keys(r.body ?? {}) };
    });
  const liveAccess = refreshed?.body?.access_token ?? accessToken;
  const transport = createConnectTransport({
    baseUrl: origin,
    httpVersion: "1.1",
    interceptors: [(next) => async (req) => {
      req.header.set("authorization", `Bearer ${liveAccess}`);
      return next(req);
    }],
  });

  return {
    dash: createClient(DashboardService, transport),
    grokBot: createClient(GrokBotService, transport),
    ai: createClient(AiService, transport),
    inference: createClient(InferenceService, transport),
  };
}

export async function ensureDevboxSandbox(grokBot, step) {
  return step("GrokBotService.EnsureSandBox", () =>
    grokBot.ensureSandBox({}),
    (r) => {
      if (!r.podId || !r.gatewayUrl || !r.gatewayToken || !r.networkToken) {
        throw new Error("EnsureSandBox returned incomplete session details");
      }
      return {
        podId: r.podId,
        cluster: r.cluster,
        hasGatewayUrl: true,
        hasGatewayToken: true,
        hasNetworkToken: true,
      };
    });
}

export function createDevboxGateway(box) {
  return {
    base: box.gatewayUrl,
    token: box.gatewayToken,
    networkToken: box.networkToken,
  };
}

export function gatewayRequest(
  gateway,
  method,
  path,
  { bearer, body, headers, signal } = {},
) {
  const requestHeaders = {
    "x-anyrun-network-token": gateway.networkToken,
    ...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
    ...(method === "POST" ? { "content-type": "application/json" } : {}),
    ...headers,
  };
  const requestBody = method !== "POST"
    ? undefined
    : body === undefined
      ? "{}"
      : typeof body === "string"
        ? body
        : JSON.stringify(body);
  return fetch(`${gateway.base}${path}`, {
    method,
    headers: requestHeaders,
    body: requestBody,
    signal,
  });
}

export function collectAssistantText(value, result = []) {
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
  if (value.kind === "send-message" &&
      value.message?.type === "text" &&
      typeof value.message.content === "string") {
    result.push(value.message.content);
  }
  for (const child of Object.values(value)) collectAssistantText(child, result);
  return result;
}
