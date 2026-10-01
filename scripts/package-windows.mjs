// Package the win32-x64 reconstructed app: rebuild the ASAR from the
// checksum-pinned Windows runtime, keep the upstream-signed exe byte for
// byte, and record a packaging manifest. Pure file ops — runs on any OS.
import { createHash } from "node:crypto";
import { cp, mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { buildAsar } from "./lib/build-asar.mjs";
import { cachedRuntimeApp, reconstructedName, runtimeResourcesDir, target, upstreamAsarSha256, windowsInstaller, windowsOutputApp } from "./lib/config.mjs";
import { validateRuntimeApp } from "./lib/runtime.mjs";

if (target !== "win32-x64") {
  throw new Error("package-windows.mjs requires GROK_BOT_TARGET=win32-x64 (use scripts/with-target.mjs)");
}

const sha256 = bytes => createHash("sha256").update(bytes).digest("hex");

async function walkFiles(root, current = root) {
  const found = [];
  for (const entry of await readdir(current, { withFileTypes: true })) {
    const targetPath = path.join(current, entry.name);
    if (entry.isDirectory()) found.push(...await walkFiles(root, targetPath));
    else if (entry.isFile()) found.push(path.relative(root, targetPath).split(path.sep).join("/"));
  }
  return found.sort();
}

const runtimeApp = await validateRuntimeApp(cachedRuntimeApp);
const exePath = path.join(runtimeApp, "Grok Bot.exe");
const exeSha256 = sha256(await readFile(exePath));

// Windows uses the plain buildAsar path: the fidelity gate pins the
// darwin-arm64 renderer, so it cannot run against the win32 payload.
const { builtAsar, builtAsarUnpacked } = await buildAsar({
  productName: reconstructedName,
});

await rm(windowsOutputApp, { recursive: true, force: true });
await mkdir(path.dirname(windowsOutputApp), { recursive: true });
await cp(runtimeApp, windowsOutputApp, { recursive: true, dereference: false, preserveTimestamps: true });

const outputResources = runtimeResourcesDir(windowsOutputApp);
await rm(path.join(outputResources, "app.asar"), { force: true });
await rm(path.join(outputResources, "app.asar.unpacked"), { recursive: true, force: true });
await cp(builtAsar, path.join(outputResources, "app.asar"));
await cp(builtAsarUnpacked, path.join(outputResources, "app.asar.unpacked"), { recursive: true, dereference: false, preserveTimestamps: true });

const outputExeSha256 = sha256(await readFile(path.join(windowsOutputApp, "Grok Bot.exe")));
if (outputExeSha256 !== exeSha256) {
  throw new Error(`Packaged exe drifted from the checksum-pinned runtime: ${outputExeSha256} != ${exeSha256}`);
}

const unpackedFiles = await walkFiles(path.join(outputResources, "app.asar.unpacked"));
const inventory = [];
for (const relative of unpackedFiles) {
  const bytes = await readFile(path.join(outputResources, "app.asar.unpacked", relative));
  inventory.push({ path: relative, bytes: bytes.byteLength, sha256: sha256(bytes) });
}
const manifest = {
  schemaVersion: 1,
  target,
  buildMode: "windows-payload-plus-reconstructed-electron-main",
  fidelityGate: "not-applicable (macOS release fidelity audit pins the darwin-arm64 renderer)",
  productName: reconstructedName,
  upstreamInstallerSha256: windowsInstaller.sha256,
  upstreamAppAsarSha256: upstreamAsarSha256,
  exeSha256: outputExeSha256,
  builtAsarSha256: sha256(await readFile(path.join(outputResources, "app.asar"))),
  unpacked: {
    fileCount: inventory.length,
    inventorySha256: sha256(JSON.stringify(inventory)),
  },
  builtAt: new Date().toISOString(),
};
await writeFile(path.join(windowsOutputApp, "reconstructed-package.json"), `${JSON.stringify(manifest, null, 2)}\n`);
console.log(`Windows package ready: ${windowsOutputApp}`);
console.log(`Built ASAR sha256: ${manifest.builtAsarSha256}`);
console.log(`Exe unchanged from runtime: ${outputExeSha256}`);
