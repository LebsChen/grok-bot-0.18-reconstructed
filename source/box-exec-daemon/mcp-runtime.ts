import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface, type Interface as ReadlineInterface } from "node:readline";

import { Struct, Value } from "@bufbuild/protobuf";

import {
  ListMcpResourcesExecResult,
  ListMcpResourcesExecResult_McpResource,
  ListMcpResourcesError,
  ListMcpResourcesSuccess,
  McpArgs,
  McpError,
  McpImageContent,
  McpResult,
  McpStateExecArgs,
  McpStateExecResult,
  McpStateServer,
  McpStateSuccess,
  McpSuccess,
  McpTextContent,
  McpToolNotFound,
  McpToolResultContentItem,
  McpServerNotFound,
  ReadMcpResourceError,
  ReadMcpResourceExecArgs,
  ReadMcpResourceExecResult,
  ReadMcpResourceNotFound,
  ReadMcpResourceSuccess,
} from "../packages/proto/generated/agent/v1/mcp_exec_pb.js";
import { McpInstructions, McpToolDefinition } from "../packages/proto/generated/agent/v1/mcp_pb.js";

const MCP_PROTOCOL_VERSION = "2024-11-05";
const MCP_REQUEST_TIMEOUT_MS = 30_000;
const MCP_MAX_LIST_PAGES = 100;

type JsonRecord = Record<string, unknown>;

interface PendingRequest {
  readonly resolve: (value: unknown) => void;
  readonly reject: (error: Error) => void;
  readonly timer: NodeJS.Timeout;
}

interface McpServerConfig {
  readonly command: string;
  readonly args: readonly string[];
  readonly env: Readonly<Record<string, string>>;
}

interface McpServerState {
  readonly name: string;
  readonly signature: string;
  readonly config: McpServerConfig;
  status: "loading" | "connected" | "error";
  errorMessage?: string;
  instructions?: string;
  capabilities: JsonRecord;
  tools: JsonRecord[];
  child?: ChildProcessWithoutNullStreams;
  lines?: ReadlineInterface;
  pending: Map<number, PendingRequest>;
  nextRequestId: number;
  startPromise?: Promise<void>;
  stopped: boolean;
  stderr: string;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function annotationsToStrings(value: unknown): Record<string, string> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      typeof item === "string" ? item : JSON.stringify(item),
    ]),
  );
}

export class McpServerRuntime {
  readonly #servers = new Map<string, McpServerState>();

  constructor(
    private readonly environment: () => NodeJS.ProcessEnv,
    private readonly workspaceRoot: string,
    private readonly writeDownload: (downloadPath: string, data: Uint8Array) => Promise<void>,
  ) {}

  async loadServers(configJson: string, removeMissing: boolean): Promise<string[]> {
    let parsed: unknown;
    try {
      parsed = JSON.parse(configJson);
    } catch (error) {
      throw new Error(`MCP server configuration is not valid JSON: ${errorMessage(error)}`);
    }
    if (!isRecord(parsed) || !isRecord(parsed.mcpServers)) {
      throw new Error("MCP server configuration must contain an mcpServers object");
    }

    const configs = new Map<string, { signature: string; config: McpServerConfig }>();
    for (const [name, rawConfig] of Object.entries(parsed.mcpServers)) {
      if (!isRecord(rawConfig) || typeof rawConfig.command !== "string" || rawConfig.command.length === 0) continue;
      const args = Array.isArray(rawConfig.args) && rawConfig.args.every((item) => typeof item === "string")
        ? rawConfig.args
        : [];
      const env = isRecord(rawConfig.env)
        ? Object.fromEntries(Object.entries(rawConfig.env).filter((entry): entry is [string, string] => typeof entry[1] === "string"))
        : {};
      const config = { command: rawConfig.command, args, env };
      configs.set(name, { signature: JSON.stringify(rawConfig), config });
    }

    if (removeMissing) {
      for (const [name, server] of this.#servers) {
        if (!configs.has(name)) {
          this.#servers.delete(name);
          await this.#stopServer(server);
        }
      }
    }

    for (const [name, entry] of configs) {
      const previous = this.#servers.get(name);
      if (previous?.signature === entry.signature) continue;
      if (previous != null) await this.#stopServer(previous);
      this.#servers.set(name, {
        name,
        signature: entry.signature,
        config: entry.config,
        status: "loading",
        capabilities: {},
        tools: [],
        pending: new Map(),
        nextRequestId: 1,
        stopped: false,
        stderr: "",
      });
    }

    return [...configs.keys()];
  }

  async state(args: McpStateExecArgs): Promise<McpStateExecResult> {
    const requested = args.serverIdentifiers.length === 0
      ? [...this.#servers.keys()]
      : args.serverIdentifiers.filter((name) => this.#servers.has(name));
    const servers: McpStateServer[] = [];
    for (const name of requested) {
      const server = this.#servers.get(name)!;
      if (args.kickOnly) {
        void this.#ensureStarted(server).catch(() => undefined);
      } else {
        try {
          await this.#ensureStarted(server);
        } catch {
          // The per-server error is returned below.
        }
      }
      servers.push(this.#stateMessage(server));
    }
    return new McpStateExecResult({
      result: { case: "success", value: new McpStateSuccess({ servers }) },
    });
  }

  async executeTool(args: McpArgs): Promise<McpResult> {
    const serverName = args.serverIdentifier || args.providerIdentifier;
    const server = this.#servers.get(serverName);
    if (server == null) {
      return new McpResult({
        result: {
          case: "serverNotFound",
          value: new McpServerNotFound({ name: serverName, availableServers: [...this.#servers.keys()] }),
        },
      });
    }

    const toolName = args.toolName || args.name;
    try {
      await this.#ensureStarted(server);
      if (!server.tools.some((tool) => tool.name === toolName)) {
        return new McpResult({
          result: {
            case: "toolNotFound",
            value: new McpToolNotFound({ name: toolName, availableTools: server.tools.map((tool) => stringValue(tool.name) ?? "") }),
          },
        });
      }
      const toolArgs = Object.fromEntries(
        Object.entries(args.args).map(([key, value]) => [key, value.toJson()]),
      );
      const result = await this.#request(server, "tools/call", { name: toolName, arguments: toolArgs });
      if (!isRecord(result)) throw new Error("MCP tools/call returned an invalid result");
      const content = Array.isArray(result.content)
        ? result.content.map((item) => this.#toolContent(item)).filter((item): item is McpToolResultContentItem => item != null)
        : [];
      const structuredContent = isRecord(result.structuredContent)
        ? Struct.fromJson(result.structuredContent as never)
        : undefined;
      return new McpResult({
        result: {
          case: "success",
          value: new McpSuccess({
            content,
            isError: result.isError === true,
            ...(structuredContent == null ? {} : { structuredContent }),
          }),
        },
      });
    } catch (error) {
      return new McpResult({
        result: { case: "error", value: new McpError({ error: errorMessage(error) }) },
      });
    }
  }

  async listResources(serverName?: string): Promise<ListMcpResourcesExecResult> {
    const names = serverName == null || serverName.length === 0
      ? [...this.#servers.keys()]
      : [serverName];
    const resources: ListMcpResourcesExecResult_McpResource[] = [];
    try {
      for (const name of names) {
        const server = this.#servers.get(name);
        if (server == null) throw new Error(`MCP server "${name}" is not configured`);
        await this.#ensureStarted(server);
        if (server.capabilities.resources == null || server.capabilities.resources === false) continue;
        for (const resource of await this.#listAll(server, "resources/list", "resources")) {
          if (!isRecord(resource) || typeof resource.uri !== "string") continue;
          resources.push(new ListMcpResourcesExecResult_McpResource({
            uri: resource.uri,
            server: name,
            ...(stringValue(resource.name) == null ? {} : { name: stringValue(resource.name)! }),
            ...(stringValue(resource.description) == null ? {} : { description: stringValue(resource.description)! }),
            ...(stringValue(resource.mimeType) == null ? {} : { mimeType: stringValue(resource.mimeType)! }),
            annotations: annotationsToStrings(resource.annotations),
          }));
        }
      }
      return new ListMcpResourcesExecResult({
        result: { case: "success", value: new ListMcpResourcesSuccess({ resources }) },
      });
    } catch (error) {
      return new ListMcpResourcesExecResult({
        result: { case: "error", value: new ListMcpResourcesError({ error: errorMessage(error) }) },
      });
    }
  }

  async readResource(args: ReadMcpResourceExecArgs): Promise<ReadMcpResourceExecResult> {
    const server = this.#servers.get(args.server);
    if (server == null) {
      return new ReadMcpResourceExecResult({
        result: {
          case: "error",
          value: new ReadMcpResourceError({ uri: args.uri, error: `MCP server "${args.server}" is not configured` }),
        },
      });
    }
    try {
      await this.#ensureStarted(server);
      if (server.capabilities.resources == null || server.capabilities.resources === false) {
        return new ReadMcpResourceExecResult({
          result: {
            case: "error",
            value: new ReadMcpResourceError({ uri: args.uri, error: `MCP server "${args.server}" does not support resources` }),
          },
        });
      }
      const result = await this.#request(server, "resources/read", { uri: args.uri });
      if (!isRecord(result) || !Array.isArray(result.contents)) {
        throw new Error("MCP resources/read returned an invalid result");
      }
      const content = result.contents.find((item) => isRecord(item) && item.uri === args.uri);
      if (!isRecord(content)) {
        return new ReadMcpResourceExecResult({
          result: { case: "notFound", value: new ReadMcpResourceNotFound({ uri: args.uri }) },
        });
      }

      let body: Uint8Array;
      let contentCase: "text" | "blob";
      let text: string | undefined;
      if (typeof content.text === "string") {
        text = content.text;
        body = new TextEncoder().encode(text);
        contentCase = "text";
      } else if (typeof content.blob === "string") {
        body = Buffer.from(content.blob, "base64");
        contentCase = "blob";
      } else {
        throw new Error("MCP resources/read content has neither text nor blob data");
      }

      if (args.downloadPath != null && args.downloadPath.length > 0) {
        await this.writeDownload(args.downloadPath, body);
      }
      return new ReadMcpResourceExecResult({
        result: {
          case: "success",
          value: new ReadMcpResourceSuccess({
            uri: args.uri,
            ...(stringValue(content.name) == null ? {} : { name: stringValue(content.name)! }),
            ...(stringValue(content.description) == null ? {} : { description: stringValue(content.description)! }),
            ...(stringValue(content.mimeType) == null ? {} : { mimeType: stringValue(content.mimeType)! }),
            annotations: annotationsToStrings(content.annotations),
            ...(args.downloadPath == null || args.downloadPath.length === 0 ? {} : { downloadPath: args.downloadPath }),
            content: contentCase === "text"
              ? { case: "text", value: text! }
              : { case: "blob", value: body },
          }),
        },
      });
    } catch (error) {
      return new ReadMcpResourceExecResult({
        result: { case: "error", value: new ReadMcpResourceError({ uri: args.uri, error: errorMessage(error) }) },
      });
    }
  }

  async stop(): Promise<void> {
    const servers = [...this.#servers.values()];
    this.#servers.clear();
    await Promise.all(servers.map((server) => this.#stopServer(server)));
  }

  #stateMessage(server: McpServerState): McpStateServer {
    const tools = server.status === "connected"
      ? server.tools.map((tool) => new McpToolDefinition({
        name: stringValue(tool.name) ?? "",
        providerIdentifier: server.name,
        toolName: stringValue(tool.name) ?? "",
        description: stringValue(tool.description) ?? "",
        ...(isRecord(tool.inputSchema) ? { inputSchema: Value.fromJson(tool.inputSchema as never) } : {}),
      }))
      : [];
    return new McpStateServer({
      serverName: server.name,
      serverIdentifier: server.name,
      tools,
      instructions: server.instructions == null || server.instructions.length === 0
        ? []
        : [new McpInstructions({
          serverName: server.name,
          serverIdentifier: server.name,
          instructions: server.instructions,
        })],
      status: server.status,
      ...(server.errorMessage == null ? {} : { errorMessage: server.errorMessage }),
    });
  }

  #toolContent(value: unknown): McpToolResultContentItem | undefined {
    if (!isRecord(value)) return undefined;
    if (value.type === "text" && typeof value.text === "string") {
      return new McpToolResultContentItem({
        content: { case: "text", value: new McpTextContent({ text: value.text }) },
      });
    }
    if (value.type === "image" && typeof value.data === "string" && typeof value.mimeType === "string") {
      return new McpToolResultContentItem({
        content: {
          case: "image",
          value: new McpImageContent({ data: Buffer.from(value.data, "base64"), mimeType: value.mimeType }),
        },
      });
    }
    return undefined;
  }

  async #ensureStarted(server: McpServerState): Promise<void> {
    if (server.stopped) throw new Error(`MCP server "${server.name}" has been stopped`);
    if (server.status === "connected" && server.child?.exitCode == null && server.child?.signalCode == null) return;
    if (server.startPromise != null) return server.startPromise;

    server.status = "loading";
    server.errorMessage = undefined;
    server.startPromise = this.#start(server)
      .catch(async (error) => {
        if (!server.stopped) {
          server.status = "error";
          server.errorMessage = errorMessage(error) || server.stderr.trim();
          await this.#stopServer(server);
          server.stopped = false;
        }
        throw error;
      })
      .finally(() => { server.startPromise = undefined; });
    return server.startPromise;
  }

  async #start(server: McpServerState): Promise<void> {
    const child = spawn(server.config.command, [...server.config.args], {
      cwd: this.workspaceRoot,
      env: { ...this.environment(), ...server.config.env },
      detached: process.platform !== "win32",
      stdio: "pipe",
    });
    server.child = child;
    server.lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
    server.lines.on("line", (line) => this.#receiveLine(server, line));
    child.stderr.on("data", (chunk) => {
      server.stderr = `${server.stderr}${String(chunk)}`.slice(-4096);
    });
    child.once("error", (error) => this.#failPending(server, error));
    child.once("exit", (code, signal) => {
      if (!server.stopped && (server.status === "connected" || server.status === "loading")) {
        const reason = `MCP server exited${code == null ? "" : ` with code ${code}`}${signal == null ? "" : ` (${signal})`}`;
        server.status = "error";
        server.errorMessage = reason;
        this.#failPending(server, new Error(reason));
      }
    });

    const initialize = await this.#request(server, "initialize", {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: "grok-bot-box-exec-daemon", version: "0.18.0" },
    });
    if (isRecord(initialize)) {
      server.capabilities = isRecord(initialize.capabilities) ? initialize.capabilities : {};
      server.instructions = stringValue(initialize.instructions);
    }
    this.#notify(server, "notifications/initialized");
    server.tools = (await this.#listAll(server, "tools/list", "tools")).filter(isRecord);
    server.status = "connected";
  }

  async #listAll(server: McpServerState, method: string, key: string): Promise<unknown[]> {
    const values: unknown[] = [];
    let cursor: string | undefined;
    for (let page = 0; page < MCP_MAX_LIST_PAGES; page += 1) {
      const result = await this.#request(server, method, cursor == null ? {} : { cursor });
      if (!isRecord(result)) throw new Error(`MCP ${method} returned an invalid result`);
      if (Array.isArray(result[key])) values.push(...result[key]);
      const nextCursor = stringValue(result.nextCursor);
      if (nextCursor == null || nextCursor.length === 0) return values;
      cursor = nextCursor;
    }
    throw new Error(`MCP ${method} exceeded ${MCP_MAX_LIST_PAGES} pages`);
  }

  #request(server: McpServerState, method: string, params: JsonRecord): Promise<unknown> {
    const child = server.child;
    if (child == null || child.exitCode != null || child.signalCode != null || server.stopped) {
      return Promise.reject(new Error(`MCP server "${server.name}" is not running`));
    }
    const id = server.nextRequestId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        server.pending.delete(id);
        reject(new Error(`MCP ${method} timed out after ${MCP_REQUEST_TIMEOUT_MS}ms`));
      }, MCP_REQUEST_TIMEOUT_MS);
      const pending = { resolve, reject, timer };
      server.pending.set(id, pending);
      try {
        child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`, (error) => {
          if (error == null) return;
          server.pending.delete(id);
          clearTimeout(timer);
          reject(error);
        });
      } catch (error) {
        server.pending.delete(id);
        clearTimeout(timer);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  #notify(server: McpServerState, method: string): void {
    const child = server.child;
    if (child == null || child.exitCode != null || child.signalCode != null || server.stopped) return;
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method })}\n`);
  }

  #receiveLine(server: McpServerState, line: string): void {
    let message: unknown;
    try {
      message = JSON.parse(line);
    } catch {
      return;
    }
    if (!isRecord(message) || typeof message.id !== "number") return;
    const pending = server.pending.get(message.id);
    if (pending == null) return;
    server.pending.delete(message.id);
    clearTimeout(pending.timer);
    if (isRecord(message.error)) {
      pending.reject(new Error(stringValue(message.error.message) ?? "MCP server returned an error"));
    } else {
      pending.resolve(message.result);
    }
  }

  #failPending(server: McpServerState, error: Error): void {
    for (const [id, pending] of server.pending) {
      clearTimeout(pending.timer);
      pending.reject(error);
      server.pending.delete(id);
    }
    if (!server.stopped && server.status === "loading") server.errorMessage = error.message;
  }

  async #stopServer(server: McpServerState): Promise<void> {
    server.stopped = true;
    server.lines?.close();
    server.lines = undefined;
    this.#failPending(server, new Error(`MCP server "${server.name}" was stopped`));
    const child = server.child;
    server.child = undefined;
    if (child == null || child.exitCode != null || child.signalCode != null) return;
    await new Promise<void>((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(forceTimer);
        resolve();
      };
      const forceTimer = setTimeout(() => {
        try {
          if (process.platform !== "win32" && child.pid != null) process.kill(-child.pid, "SIGKILL");
          else child.kill("SIGKILL");
        } catch {}
        finish();
      }, 1000);
      child.once("close", finish);
      try {
        if (process.platform !== "win32" && child.pid != null) process.kill(-child.pid, "SIGTERM");
        else child.kill("SIGTERM");
      } catch {
        finish();
      }
    });
  }
}
