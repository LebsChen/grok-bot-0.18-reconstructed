import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
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

async function withRuntime(callback, environment = {}) {
  const { module } = await loadRuntimeModule();
  const workspace = await mkdtemp(path.join(os.tmpdir(), "grok-box-exec-workspace-"));
  const terminals = path.join(workspace, "terminals");
  await mkdir(terminals, { recursive: true });
  const [workspaceRoot, terminalsDirectory] = await Promise.all([realpath(workspace), realpath(terminals)]);
  const runtime = new module.BoxExecRuntime(workspaceRoot, terminalsDirectory, environment);
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
    await callback({ runtime, workspace: workspaceRoot });
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
    assert.equal(response.value.result.case, "success", JSON.stringify(response.value.result));
    const result = response.value.result.value;
    assert.equal(result.uri, "test://resource");
    assert.equal(result.content.case, "text");
    assert.equal(result.content.value, "resource:test://resource");
    assert.equal(result.downloadPath, "downloaded-resource.txt");
    assert.equal(await readFile(path.join(workspace, "downloaded-resource.txt"), "utf8"), "resource:test://resource");
  });
});

test("computerUseArgs returns an error result when DISPLAY is unset", async () => {
  await withRuntime(async ({ runtime }) => {
    const response = await execute(runtime, "computerUseArgs", { actions: [] });
    assert.equal(response.case, "computerUseResult");
    assert.equal(response.value.result.case, "error");
    assert.match(response.value.result.value.error, /DISPLAY/);
    assert.equal(response.value.result.value.actionCount, 0);
  });
  const { module } = await loadRuntimeModule();
  assert.equal(await module.detectComputerUseSupport({ PATH: "/usr/bin" }), false);
});

const TINY_WEBP_BASE64 = Buffer.from(
  "RIFF\x14\x00\x00\x00WEBPVP8 \x08\x00\x00\x00fake", "binary").toString("base64");

async function withFakeComputerUseBin(callback, { failArg } = {}) {
  const bin = await mkdtemp(path.join(os.tmpdir(), "grok-box-exec-bin-"));
  const logPath = path.join(bin, "invocations.log");
  const systemPath = process.platform === "win32"
    ? process.env.SystemRoot ? `${process.env.SystemRoot}\\System32` : "C:\\Windows\\System32"
    : "/usr/bin:/bin";
  if (process.platform === "win32") {
    const tinyWebp = path.join(bin, "tiny.webp");
    await writeFile(tinyWebp, Buffer.from(TINY_WEBP_BASE64, "base64"));
    await writeFile(path.join(bin, "xdotool.cmd"), [
      "@echo off",
      `echo xdotool %* >>"${logPath}"`,
      'if "%1"=="getmouselocation" (echo X=12&echo Y=34&echo SCREEN=0&echo WINDOW=1)',
      'if defined CU_FAIL_ARG if "%1"=="%CU_FAIL_ARG%" (echo boom-%1 1>&2&exit /b 1)',
      "exit /b 0",
    ].join("\r\n"));
    await writeFile(path.join(bin, "import.cmd"), [
      "@echo off",
      `echo import %* >>"${logPath}"`,
      `type "${tinyWebp}"`,
      "exit /b 0",
    ].join("\r\n"));
  } else {
    const xdotool = [
      "#!/bin/sh",
      `echo xdotool "$@" >> "${logPath}"`,
      'if [ "$1" = "getmouselocation" ]; then printf "X=12\\nY=34\\nSCREEN=0\\nWINDOW=1\\n"; fi',
      `if [ -n "$CU_FAIL_ARG" ] && [ "$1" = "$CU_FAIL_ARG" ]; then echo "boom-$1" >&2; exit 1; fi`,
      "exit 0",
    ].join("\n");
    const importTool = [
      "#!/bin/sh",
      `echo import "$@" >> "${logPath}"`,
      `printf '${TINY_WEBP_BASE64}' | base64 -d`,
      "exit 0",
    ].join("\n");
    await writeFile(path.join(bin, "xdotool"), xdotool, { mode: 0o755 });
    await writeFile(path.join(bin, "import"), importTool, { mode: 0o755 });
  }
  const environment = {
    DISPLAY: ":0",
    PATH: `${bin}${path.delimiter}${systemPath}`,
    XDOTOOL_LOG: logPath,
    ...(failArg ? { CU_FAIL_ARG: failArg } : {}),
  };
  const readLog = async () =>
    (await readFile(logPath, "utf8").catch(() => ""))
      .split("\n").map(line => line.trim()).filter(Boolean);
  try {
    await callback({ bin, logPath, environment, readLog });
  } finally {
    await rm(bin, { recursive: true, force: true });
  }
}

const mouseAction = (name, value) => ({ action: { case: name, value } });

test("computerUseArgs maps click modifiers, count, and captures a webp screenshot", async () => {
  await withFakeComputerUseBin(async ({ environment, readLog }) => {
    await withRuntime(async ({ runtime }) => {
      const response = await execute(runtime, "computerUseArgs", {
        actions: [mouseAction("click", {
          coordinate: { x: 100, y: 200 },
          button: 1, count: 2, modifierKeys: "ctrl+shift",
        })],
      });
      assert.equal(response.value.result.case, "success");
      const result = response.value.result.value;
      assert.equal(result.actionCount, 1);
      assert.ok(result.durationMs >= 0);
      const decoded = Buffer.from(result.screenshot, "base64");
      assert.equal(decoded.subarray(0, 4).toString("latin1"), "RIFF");
      assert.equal(decoded.subarray(8, 12).toString("latin1"), "WEBP");
      assert.deepEqual({ x: result.cursorPosition.x, y: result.cursorPosition.y },
                       { x: 12, y: 34 });
      const log = await readLog();
      assert.deepEqual(log.filter(line => line.startsWith("xdotool")), [
        "xdotool keydown ctrl",
        "xdotool keydown shift",
        "xdotool mousemove 100 200",
        "xdotool click --repeat 2 1",
        "xdotool keyup shift",
        "xdotool keyup ctrl",
        "xdotool getmouselocation --shell",
      ]);
      assert.deepEqual(log.filter(line => line.startsWith("import")),
                       ["import -window root webp:-"]);
    }, environment);
  });
});

test("computerUseArgs maps drag, scroll, type, key-hold, and wait clamp", async () => {
  const { module } = await loadRuntimeModule();
  await withFakeComputerUseBin(async ({ environment }) => {
    const argv = [];
    const sleeps = [];
    const result = await module.executeComputerUse({
      actions: [
        mouseAction("drag", { path: [{ x: 1, y: 2 }, { x: 3, y: 4 }, { x: 5, y: 6 }],
                              button: 1, modifierKeys: "alt" }),
        mouseAction("scroll", { coordinate: { x: 9, y: 9 }, direction: 2, amount: 3 }),
        mouseAction("type", { text: "hello world" }),
        mouseAction("key", { key: "ctrl+escape", holdDurationMs: 250 }),
        mouseAction("wait", { durationMs: 99_999 }),
      ],
    }, {
      environment,
      runCommand: async (callArgv) => {
        argv.push(callArgv);
        if (callArgv[0] === "xdotool" && callArgv[1] === "getmouselocation") {
          return { code: 0, stdout: "X=5\nY=6\n", stderr: "", stdoutBytes: Buffer.alloc(0) };
        }
        if (callArgv[0] === "import") {
          return { code: 0, stdout: "", stderr: "",
                   stdoutBytes: Buffer.from(TINY_WEBP_BASE64, "base64") };
        }
        return { code: 0, stdout: "", stderr: "", stdoutBytes: Buffer.alloc(0) };
      },
      sleep: async (ms) => { sleeps.push(ms); },
      now: () => Date.now(),
    });
    assert.equal(result.result.case, "success", JSON.stringify(result.toJson()));
    assert.ok(sleeps.includes(30_000));
    assert.ok(sleeps.includes(250));
    const xdo = argv.filter(call => call[0] === "xdotool").map(call => call.slice(1));
    assert.deepEqual(xdo, [
      ["keydown", "alt"],
      ["mousemove", "1", "2"],
      ["mousedown", "1"],
      ["mousemove", "3", "4"],
      ["mousemove", "5", "6"],
      ["mouseup", "1"],
      ["keyup", "alt"],
      ["mousemove", "9", "9"],
      ["click", "--repeat", "3", "5"],
      ["type", "--delay", "12", "--", "hello world"],
      ["keydown", "ctrl"],
      ["keydown", "Escape"],
      ["keyup", "Escape"],
      ["keyup", "ctrl"],
      ["getmouselocation", "--shell"],
    ]);
    assert.equal(result.result.value.actionCount, 5);
  });
});

test("computerUseArgs reports the completed action count on mid-sequence failure", async () => {
  const { module } = await loadRuntimeModule();
  await withFakeComputerUseBin(async ({ environment }) => {
    const result = await module.executeComputerUse({
      actions: [
        mouseAction("mouseMove", { coordinate: { x: 1, y: 1 } }),
        mouseAction("type", { text: "x" }),
        mouseAction("type", { text: "never reached" }),
      ],
    }, {
      environment,
      runCommand: async (callArgv) => {
        if (callArgv[1] === "type") {
          return { code: 1, stdout: "", stderr: "type blew up",
                   stdoutBytes: Buffer.alloc(0) };
        }
        return { code: 0, stdout: "", stderr: "", stdoutBytes: Buffer.alloc(0) };
      },
      sleep: async () => {},
      now: () => Date.now(),
    });
    assert.equal(result.result.case, "error");
    assert.equal(result.result.value.actionCount, 1);
    assert.match(result.result.value.error, /type blew up/);
  });
});

test("computerUseArgs errors without DISPLAY and detectComputerUseSupport agrees", async () => {
  const { module } = await loadRuntimeModule();
  assert.equal(await module.detectComputerUseSupport({ PATH: "/usr/bin:/bin" }), false);
  const result = await module.executeComputerUse({ actions: [] }, {
    environment: { PATH: "/usr/bin:/bin" },
    runCommand: async () => { throw new Error("must not run"); },
    sleep: async () => {},
    now: () => Date.now(),
  });
  assert.equal(result.result.case, "error");
  assert.match(result.result.value.error, /DISPLAY/);
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
