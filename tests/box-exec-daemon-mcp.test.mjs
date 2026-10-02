import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
let loadedModule;

async function loadRuntimeModule() {
  if (loadedModule != null) return loadedModule;
  const temporary = await mkdtemp(path.join(os.tmpdir(), "grok-box-exec-mcp-"));
  const output = path.join(temporary, "box-exec-daemon.cjs");
  await build({
    entryPoints: [path.join(repoRoot, "source/box-exec-daemon/server.ts")],
    outfile: output,
    bundle: true,
    format: "cjs",
    platform: "node",
    target: "node22",
  });
  loadedModule = {
    module: require(output),
    dispose: () => rm(temporary, { recursive: true, force: true }),
  };
  return loadedModule;
}

after(async () => {
  await loadedModule?.dispose();
});

const fixtureServerScript = [
  'let buffer = "";',
  'const send = value => process.stdout.write(JSON.stringify(value) + "\\n");',
  'process.stdin.setEncoding("utf8");',
  'process.stdin.on("data", chunk => {',
  '  buffer += chunk;',
  '  let newline;',
  '  while ((newline = buffer.indexOf("\\n")) >= 0) {',
  '    const line = buffer.slice(0, newline);',
  '    buffer = buffer.slice(newline + 1);',
  '    const request = JSON.parse(line);',
  '    let result;',
  '    if (request.method === "initialize") {',
  '      result = { protocolVersion: "2024-11-05", capabilities: { tools: {}, resources: {} }, serverInfo: { name: "fixture", version: "1" }, instructions: "fixture instructions" };',
  '    } else if (request.method === "tools/list") {',
  '      result = { tools: [{ name: "echo", description: "Echo text", inputSchema: { type: "object", properties: { text: { type: "string" } } } }] };',
  '    } else if (request.method === "tools/call") {',
  '      result = { content: [{ type: "text", text: "echo:" + request.params.arguments.text }], structuredContent: { echoed: request.params.arguments.text } };',
  '    } else if (request.method === "resources/list") {',
  '      result = { resources: [{ uri: "test://resource", name: "fixture-resource", mimeType: "text/plain", annotations: { audience: ["user"] } }] };',
  '    } else if (request.method === "resources/read") {',
  '      result = { contents: [{ uri: request.params.uri, mimeType: "text/plain", text: "resource:" + request.params.uri }] };',
  '    } else {',
  '      send({ jsonrpc: "2.0", id: request.id, error: { code: -32601, message: "Unknown method" } });',
  '      continue;',
  '    }',
  '    if (request.id !== undefined) send({ jsonrpc: "2.0", id: request.id, result });',
  '  }',
  '});',
].join("\n");

async function withRuntime(callback) {
  const { module } = await loadRuntimeModule();
  const workspace = await mkdtemp(path.join(os.tmpdir(), "grok-box-exec-workspace-"));
  const runtime = new module.BoxExecRuntime(workspace, path.join(workspace, "terminals"), {});
  const loaded = await runtime.loadMcpServers({
    mcpConfigJson: JSON.stringify({
      mcpServers: {
        fixture: {
          command: process.execPath,
          args: ["-e", fixtureServerScript],
        },
      },
    }),
    removeMissing: true,
  });
  try {
    assert.deepEqual(loaded.loadedServerNames, ["fixture"]);
    await callback({ runtime, workspace });
  } finally {
    await runtime.stop();
    await rm(workspace, { recursive: true, force: true });
  }
}

async function execute(runtime, messageCase, value) {
  const output = [];
  for await (const element of runtime.execute({
    id: 7,
    execId: "mcp-test",
    message: { case: messageCase, value },
  }, new AbortController().signal)) {
    output.push(element);
  }
  const item = output.find((element) => element.element.case === "execClientMessage");
  assert.ok(item, `expected an ExecClientMessage for ${messageCase}`);
  return item.element.value.message;
}

test("mcpStateExecArgs starts a configured stdio server and returns host-compatible tools", async () => {
  await withRuntime(async ({ runtime }) => {
    const response = await execute(runtime, "mcpStateExecArgs", {
      serverIdentifiers: ["fixture"],
      kickOnly: false,
    });
    assert.equal(response.case, "mcpStateExecResult");
    assert.equal(response.value.result.case, "success");
    const [server] = response.value.result.value.servers;
    assert.equal(server.serverIdentifier, "fixture");
    assert.equal(server.status, "connected");
    assert.equal(server.instructions[0].instructions, "fixture instructions");
    assert.deepEqual(server.tools.map((tool) => ({
      name: tool.name,
      providerIdentifier: tool.providerIdentifier,
      toolName: tool.toolName,
      description: tool.description,
    })), [{
      name: "echo",
      providerIdentifier: "fixture",
      toolName: "echo",
      description: "Echo text",
    }]);
    assert.deepEqual(server.tools[0].inputSchema.toJson(), {
      type: "object",
      properties: { text: { type: "string" } },
    });
  });
});

test("mcpArgs calls a stdio tool and maps MCP content and structured content", async () => {
  await withRuntime(async ({ runtime }) => {
    const response = await execute(runtime, "mcpArgs", {
      name: "fixture-echo",
      serverIdentifier: "fixture",
      providerIdentifier: "fixture",
      toolName: "echo",
      args: { text: { toJson: () => "hello" } },
    });
    assert.equal(response.case, "mcpResult");
    assert.equal(response.value.result.case, "success");
    const result = response.value.result.value;
    assert.equal(result.content[0].content.case, "text");
    assert.equal(result.content[0].content.value.text, "echo:hello");
    assert.deepEqual(result.structuredContent.toJson(), { echoed: "hello" });
  });
});

test("listMcpResourcesExecArgs lists server resources with their server identifier", async () => {
  await withRuntime(async ({ runtime }) => {
    const response = await execute(runtime, "listMcpResourcesExecArgs", { server: "fixture" });
    assert.equal(response.case, "listMcpResourcesExecResult");
    assert.equal(response.value.result.case, "success");
    const [resource] = response.value.result.value.resources;
    assert.equal(resource.server, "fixture");
    assert.equal(resource.uri, "test://resource");
    assert.equal(resource.name, "fixture-resource");
    assert.equal(resource.mimeType, "text/plain");
    assert.deepEqual(resource.annotations, { audience: '["user"]' });
  });
});

test("readMcpResourceExecArgs returns resource content and writes an optional download", async () => {
  await withRuntime(async ({ runtime, workspace }) => {
    const response = await execute(runtime, "readMcpResourceExecArgs", {
      server: "fixture",
      uri: "test://resource",
      downloadPath: "downloaded-resource.txt",
    });
    assert.equal(response.case, "readMcpResourceExecResult");
    assert.equal(response.value.result.case, "success");
    const result = response.value.result.value;
    assert.equal(result.uri, "test://resource");
    assert.equal(result.content.case, "text");
    assert.equal(result.content.value, "resource:test://resource");
    assert.equal(result.downloadPath, "downloaded-resource.txt");
    assert.equal(await readFile(path.join(workspace, "downloaded-resource.txt"), "utf8"), "resource:test://resource");
  });
});

test("computerUseArgs returns the proto error result when UI control is unavailable", async () => {
  await withRuntime(async ({ runtime }) => {
    const response = await execute(runtime, "computerUseArgs", { actions: [] });
    assert.equal(response.case, "computerUseResult");
    assert.equal(response.value.result.case, "error");
    assert.equal(response.value.result.value.error, "Computer use is not available in this runtime.");
    assert.equal(response.value.result.value.actionCount, 0);
    assert.equal(response.value.result.value.durationMs, 0);
  });
});

test("unsupported ExecServerMessage logging excludes request payloads", async () => {
  await withRuntime(async ({ runtime }) => {
    const chunks = [];
    const originalWrite = process.stderr.write;
    process.stderr.write = function (chunk, encoding, callback) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk.toString() : String(chunk));
      const done = typeof encoding === "function" ? encoding : callback;
      done?.();
      return true;
    };
    let output;
    try {
      output = [];
      for await (const element of runtime.execute({
        id: 7,
        execId: "unsupported-test",
        message: {
          case: "futureExecArgs",
          value: { apiKey: "must-not-be-logged" },
        },
      }, new AbortController().signal)) {
        output.push(element);
      }
    } finally {
      process.stderr.write = originalWrite;
    }

    assert.deepEqual(JSON.parse(chunks.join("")), {
      case: "futureExecArgs",
      requestId: 7,
    });
    assert.equal(
      output.find((element) => element.element.case === "execClientControlMessage")
        .element.value.message.case,
      "throw",
    );
  });
});
