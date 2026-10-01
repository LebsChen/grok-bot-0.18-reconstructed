// Dump every proto message/enum/field reachable from the services DevBox
// serves, into a JSON the control plane turns into a descriptor_pool.
// Usage: node tools/gen_grokbot_descriptors.mjs [output]
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const botDir = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const repoRoot = process.env.GROKBOT_FORK_ROOT ?? botDir;
const generatedRoot = path.join(repoRoot, "source/packages/proto/generated");
const outPath = process.argv[2] ?? path.join(botDir, "devbox_bot", "descriptors.json");

const compileRoot = mkdtempSync(path.join(repoRoot, ".tmp-gb-proto-"));
writeFileSync(path.join(compileRoot, "package.json"), '{"type":"module"}\n');
execFileSync("bash", ["-lc", `
  cd ${JSON.stringify(generatedRoot)} &&
  find . -name '*.ts' -print0 | xargs -0 ${JSON.stringify(path.join(repoRoot, "node_modules/.bin/esbuild"))} \
    --format=esm --platform=node --outdir=${JSON.stringify(compileRoot)}
`], { stdio: "inherit" });

const SCALAR = {
  1: "double", 2: "float", 3: "int64", 4: "uint64", 5: "int32", 6: "fixed64",
  7: "fixed32", 8: "bool", 9: "string", 12: "bytes", 13: "uint32",
  15: "sfixed32", 16: "sfixed64", 17: "sint32", 18: "sint64",
};

const services = [];
for (const connectFile of process.argv.slice(3).length
    ? process.argv.slice(3)
    : ["aiserver/v1/aiserver_connect.js", "aiserver/v1/dashboard_connect.js",
       "aiserver/v1/grok_bot_connect.js", "aiserver/v1/inference_connect.js",
       "aiserver/v1/analytics_connect.js", "agent/v1/agent_service_connect.js"]) {
  const mod = await import(pathToFileURL(path.join(compileRoot, connectFile.replace(/\.mjs$/, ".js"))).href);
  for (const value of Object.values(mod)) {
    if (value == null || typeof value !== "object" || typeof value.typeName !== "string"
        || value.methods == null) continue;
    const methods = {};
    for (const [key, m] of Object.entries(value.methods)) {
      methods[key] = {
        name: m.name,
        kind: typeof m.kind === "number" ? m.kind : String(m.kind),
        input: m.I?.typeName,
        output: m.O?.typeName,
      };
    }
    services.push({ typeName: value.typeName, methods });
  }
}

// Collect every message type reachable from service I/O types.
const messages = {};
const enums = {};
const queue = [];
const typeModules = new Map();
async function loadType(typeName) {
  if (messages[typeName] || queue.includes(typeName)) return;
  queue.push(typeName);
}
// Seed from service I/O type names -> find them by scanning pb modules.
import { readdirSync } from "node:fs";
function listPbFiles(dir, prefix = "") {
  const files = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) files.push(...listPbFiles(path.join(dir, entry.name), rel));
    else if (entry.name.endsWith("_pb.js")) files.push(rel);
  }
  return files;
}
const pbFiles = listPbFiles(compileRoot);
const exported = {};
for (const pb of pbFiles) {
  try {
    const mod = await import(pathToFileURL(path.join(compileRoot, pb)).href);
    // pb list entries already carry the compiled .js extension
    exported[pb] = mod;
  } catch (error) {
    console.error(`skip ${pb}: ${error.message}`);
  }
}
const typeByName = new Map();
// exported enums are plain reverse-mapped objects; index each module's
// enum exports by their value-name signature so field EnumType objects can
// be attributed to the file that defines them
const enumBySig = new Map();
for (const [pb, mod] of Object.entries(exported)) {
  for (const [exportName, value] of Object.entries(mod)) {
    if (value && (typeof value === "object" || typeof value === "function")
        && typeof value.typeName === "string"
        && value.fields != null && typeof value.fields.list === "function") {
      typeByName.set(value.typeName, { type: value, file: pb });
    }
    if (value && typeof value === "object" && !Array.isArray(value)
        && value.typeName == null && value.fields == null
        && exportName !== "proto3") {
      const names = Object.keys(value).filter(k => Number.isNaN(Number(k)));
      const numericKeys = Object.keys(value).filter(k => !Number.isNaN(Number(k)));
      if (names.length && numericKeys.length === names.length
          && numericKeys.every(k => typeof value[k] === "string"
              && value[value[k]] === Number(k))) {
        const sig = numericKeys.map(Number).sort((a, b) => a - b)
            .map(no => `${no}:${value[no]}`).join(",");
        const list = enumBySig.get(`sig::${sig}`) ?? [];
        if (!list.includes(pb)) { list.push(pb); enumBySig.set(`sig::${sig}`, list); }
      }
    }
  }
}
for (const svc of services) {
  for (const m of Object.values(svc.methods)) {
    await loadType(m.input);
    await loadType(m.output);
  }
}
while (queue.length) {
  const typeName = queue.shift();
  if (messages[typeName]) continue;
  const hit = typeByName.get(typeName);
  if (!hit) { messages[typeName] = { typeName, file: null, missing: true, fields: [] }; continue; }
  const fields = [];
  for (const f of hit.type.fields.list()) {
    const entry = {
      no: f.no,
      name: f.name,
      localName: f.localName,
      kind: f.kind,
      opt: f.opt === true,
      repeated: f.repeated === true,
      oneof: f.oneof == null ? null : (typeof f.oneof === "string" ? f.oneof : f.oneof.name),
      fieldKind: f.fieldKind ?? null,
    };
    if (f.kind === "scalar") entry.T = SCALAR[f.T] ?? f.T;
    else if (f.kind === "message") { entry.T = f.T.typeName; await loadType(f.T.typeName); }
    else if (f.kind === "enum") {
      entry.T = f.T.typeName;
      const sig = (f.T.values ?? []).map(v => `${v.no}:${v.localName ?? v.name}`)
          .sort((a, b) => parseInt(a) - parseInt(b)).join(",");
      const candidates = enumBySig.get(`sig::${sig}`) ?? [];
      const enumFile = (candidates.includes(hit.file) ? hit.file : candidates[0])
          ?? hit.file;
      enums[f.T.typeName] ??= {
        typeName: f.T.typeName,
        file: enumFile.replace(/\.js$/, ".proto"),
        values: (f.T.values ?? []).map(v => ({ no: v.no, name: v.name, localName: v.localName })),
      };
    }
    else if (f.kind === "map") {
      const valueType = f.V?.kind === "message"
        ? (await loadType(f.V.T.typeName), f.V.T.typeName)
        : (f.V?.kind === "enum" ? f.V.T.typeName : SCALAR[f.V?.T] ?? f.V?.T);
      if (f.V?.kind === "enum") {
        const sig = (f.V.T.values ?? []).map(v => `${v.no}:${v.localName ?? v.name}`)
            .sort((a, b) => parseInt(a) - parseInt(b)).join(",");
        const candidates = enumBySig.get(`sig::${sig}`) ?? [];
        const enumFile = (candidates.includes(hit.file) ? hit.file : candidates[0])
            ?? hit.file;
        enums[f.V.T.typeName] ??= {
          typeName: f.V.T.typeName,
          file: enumFile.replace(/\.js$/, ".proto"),
          values: (f.V.T.values ?? []).map(v => ({ no: v.no, name: v.name, localName: v.localName })),
        };
      }
      entry.T = { key: SCALAR[f.K] ?? f.K, value: valueType, valueKind: f.V?.kind ?? "scalar" };
    }
    fields.push(entry);
  }
  messages[typeName] = { typeName, file: hit.file.replace(/\.js$/, ".proto"), fields };
}

const out = {
  generated: new Date().toISOString(),
  services,
  messages,
  enums,
};
writeFileSync(outPath, `${JSON.stringify(out, null, 1)}\n`);
console.log(`wrote ${outPath}: ${services.length} services, ${Object.keys(messages).length} messages, ${Object.keys(enums).length} enums`);
rmSync(compileRoot, { recursive: true, force: true });
