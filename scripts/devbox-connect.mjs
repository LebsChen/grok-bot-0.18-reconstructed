// Connect the Windows build to a DevBox session's box host: mint
// preview-link capabilities for ports 1340/1341, read the descriptor
// for the gateway token, then launch `Grok Bot.exe` with the gateway
// env. Token values are never logged.
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { windowsOutputApp } from "./lib/config.mjs";

export function capabilityFromUrl(rawUrl) {
  const parsed = new URL(rawUrl);
  const token = parsed.searchParams.get("tkn");
  return { capability: token, baseUrl: `${parsed.origin}${parsed.pathname}` };
}

export async function mintPreviewLink({ origin, sessionId, apiKey, localPort, fetchImpl = fetch }) {
  const url = `${origin}/api/preview-link/${sessionId}?local_port=${localPort}`;
  const response = await fetchImpl(url, {
    method: "PUT",
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  if (!response.ok) {
    throw new Error(`preview-link port ${localPort} failed: HTTP ${response.status}`);
  }
  const body = await response.json();
  const { capability, baseUrl } = capabilityFromUrl(body.url ?? "");
  if (!capability) {
    throw new Error(`preview-link port ${localPort} returned no tkn capability; keys: ${Object.keys(body).join(", ")}`);
  }
  return { capability, baseUrl };
}

export async function fetchDescriptor({ descriptorBase, capability, fetchImpl = fetch }) {
  const response = await fetchImpl(`${descriptorBase}/descriptor`, {
    headers: { "x-anyrun-network-token": capability },
  });
  if (!response.ok) {
    throw new Error(`descriptor fetch failed: HTTP ${response.status}`);
  }
  return response.json();
}

function parseArgs(argv) {
  const options = { printEnvOnly: false };
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--origin") options.origin = argv[++index];
    else if (argv[index] === "--session") options.sessionId = argv[++index];
    else if (argv[index] === "--app") options.app = argv[++index];
    else if (argv[index] === "--print-env-only") options.printEnvOnly = true;
    else throw new Error(`unknown argument: ${argv[index]}`);
  }
  return options;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const origin = options.origin ?? process.env.DEVBOX_ORIGIN ?? "https://app.devinai.net";
  const sessionId = options.sessionId ?? process.env.DEVBOX_SESSION_ID;
  const apiKey = process.env.DEVBOX_API_KEY;
  if (!sessionId) throw new Error("--session or DEVBOX_SESSION_ID is required");
  if (!apiKey) throw new Error("DEVBOX_API_KEY is required");
  const appExe = options.app ?? path.join(windowsOutputApp, "Grok Bot.exe");

  const gateway = await mintPreviewLink({ origin, sessionId, apiKey, localPort: 1340 });
  const descriptor = await mintPreviewLink({ origin, sessionId, apiKey, localPort: 1341 });
  const descriptorPayload = await fetchDescriptor({
    descriptorBase: descriptor.baseUrl,
    capability: descriptor.capability,
  });
  if (!descriptorPayload.gatewayToken) {
    throw new Error("descriptor response had no gatewayToken");
  }

  const health = await fetch(`${gateway.baseUrl}/health`, {
    headers: { "x-anyrun-network-token": gateway.capability },
  });
  if (!health.ok) {
    throw new Error(`gateway /health failed: HTTP ${health.status}`);
  }

  const env = {
    SAND_HOST_GATEWAY_URL: gateway.baseUrl.replace(/\/$/, ""),
    SAND_HOST_GATEWAY_TOKEN: descriptorPayload.gatewayToken,
    SAND_HOST_GATEWAY_NETWORK_TOKEN: gateway.capability,
  };
  if (options.printEnvOnly) {
    for (const name of Object.keys(env)) console.log(`${name}=<redacted>`);
    return;
  }
  console.log(`Launching ${appExe} against the session gateway (env redacted)`);
  const child = spawn(appExe, ["--remote-debugging-port=0"], {
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
