import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { constants } from "node:fs";
import { access } from "node:fs/promises";

import {
  Coordinate,
  ComputerUseError,
  ComputerUseResult,
  ComputerUseSuccess,
  MouseButton,
  ScrollDirection,
  type ComputerUseAction,
  type ComputerUseArgs,
} from "../packages/proto/generated/agent/v1/computer_use_tool_pb.js";
import { COMPUTER_USE_SCREENSHOT_SETTLE_DELAY_MS } from "../packages/agent-exec/computer-use.js";

const PACKAGED_XDOTOOL = "/opt/.devbox/package/custom_binaries/xdotool";
const WAIT_MAX_MS = 30_000;
const COMMAND_TIMEOUT_MS = 15_000;
const STDERR_TAIL_CHARS = 2_000;

const KEY_ALIASES: Record<string, string> = {
  return: "Return", enter: "Return", esc: "Escape",
  escape: "Escape", tab: "Tab", backspace: "BackSpace",
  space: "space", left: "Left", right: "Right",
  up: "Up", down: "Down", home: "Home", end: "End",
  delete: "Delete", del: "Delete", pageup: "Prior",
  pagedown: "Next", shift: "shift", ctrl: "ctrl",
  control: "ctrl", alt: "alt", meta: "meta",
  super: "super", windows: "super",
  arrowleft: "Left", arrowright: "Right",
  arrowup: "Up", arrowdown: "Down",
};

function normalizeKeyName(name: string): string {
  const value = String(name).trim();
  if (!value) throw new Error("key name must not be empty");
  return KEY_ALIASES[value.toLowerCase()] ?? value;
}

export function normalizeXdotoolKeySequence(sequence: string): string {
  const parts = String(sequence).split("+");
  if (!parts.length || parts.some(part => !part.trim())) {
    throw new Error("key sequence must not be empty");
  }
  return parts.map(normalizeKeyName).join("+");
}

const MOUSE_BUTTON_X11: Record<number, number> = {
  [MouseButton.UNSPECIFIED]: 1,
  [MouseButton.LEFT]: 1,
  [MouseButton.MIDDLE]: 2,
  [MouseButton.RIGHT]: 3,
  [MouseButton.BACK]: 8,
  [MouseButton.FORWARD]: 9,
};

const SCROLL_BUTTON_X11: Record<number, number> = {
  [ScrollDirection.UNSPECIFIED]: 4,
  [ScrollDirection.UP]: 4,
  [ScrollDirection.DOWN]: 5,
  [ScrollDirection.LEFT]: 6,
  [ScrollDirection.RIGHT]: 7,
};

export interface ComputerUseCommandOutcome {
  readonly code: number;
  readonly stdout: string;
  readonly stderr: string;
  readonly stdoutBytes: Buffer;
}

export type ComputerUseRunCommand =
  (argv: string[], environment: NodeJS.ProcessEnv) => Promise<ComputerUseCommandOutcome>;

export interface ComputerUseDeps {
  readonly environment: NodeJS.ProcessEnv;
  readonly runCommand?: ComputerUseRunCommand;
  readonly sleep?: (ms: number) => Promise<void>;
  readonly now?: () => number;
}

async function defaultRunCommand(argv: string[], environment: NodeJS.ProcessEnv): Promise<ComputerUseCommandOutcome> {
  const [command, ...rest] = argv;
  if (!command) return Promise.reject(new Error("empty command argv"));
  return new Promise((resolve, reject) => {
    const child = spawn(command, rest, { env: environment }) as ChildProcessWithoutNullStreams;
    const stdoutChunks: Buffer[] = [];
    const stderrChunks: Buffer[] = [];
    const timer = setTimeout(() => child.kill("SIGKILL"), COMMAND_TIMEOUT_MS);
    child.stdout.on("data", data => { stdoutChunks.push(Buffer.from(data)); });
    child.stderr.on("data", data => { stderrChunks.push(Buffer.from(data)); });
    child.once("error", error => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("close", code => {
      clearTimeout(timer);
      const stdoutBytes = Buffer.concat(stdoutChunks);
      resolve({
        code: code ?? 1,
        stdout: stdoutBytes.toString("utf8"),
        stderr: Buffer.concat(stderrChunks).toString("utf8"),
        stdoutBytes,
      });
    });
  });
}

const defaultSleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

async function executableOnPath(name: string, environment: NodeJS.ProcessEnv): Promise<boolean> {
  if (name.includes("/")) {
    try {
      await access(name, constants.X_OK);
      return true;
    } catch {
      return false;
    }
  }
  for (const directory of String(environment.PATH ?? "").split(":")) {
    if (!directory) continue;
    try {
      await access(`${directory}/${name}`, constants.X_OK);
      return true;
    } catch {}
  }
  return false;
}

export interface ComputerUseToolchain {
  readonly xdotool: string;
  readonly capturer: "import" | "ffmpeg";
}

export async function resolveComputerUseToolchain(
  environment: NodeJS.ProcessEnv,
): Promise<ComputerUseToolchain | undefined> {
  const display = String(environment.DISPLAY ?? "").trim();
  if (!display) return undefined;
  const xdotool = await executableOnPath(PACKAGED_XDOTOOL, environment)
    ? PACKAGED_XDOTOOL
    : await executableOnPath("xdotool", environment) ? "xdotool" : "";
  if (!xdotool) return undefined;
  const capturer = await executableOnPath("import", environment)
    ? "import" as const
    : await executableOnPath("ffmpeg", environment) ? "ffmpeg" as const : undefined;
  if (!capturer) return undefined;
  return { xdotool, capturer };
}

export async function detectComputerUseSupport(
  environment: NodeJS.ProcessEnv,
): Promise<boolean> {
  return (await resolveComputerUseToolchain(environment)) !== undefined;
}

class ComputerUseActionFailure extends Error {
  constructor(message: string, readonly log: string) {
    super(message);
  }
}

function buttonArg(button: MouseButton): number {
  return MOUSE_BUTTON_X11[button] ?? 1;
}

async function xdotool(
  toolchain: ComputerUseToolchain,
  deps: Required<Pick<ComputerUseDeps, "runCommand">> & Pick<ComputerUseDeps, "environment">,
  args: string[],
): Promise<ComputerUseCommandOutcome> {
  let outcome: ComputerUseCommandOutcome;
  try {
    outcome = await deps.runCommand([toolchain.xdotool, ...args], deps.environment);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new ComputerUseActionFailure(`xdotool ${args[0] ?? ""} failed to launch: ${message}`, message);
  }
  if (outcome.code !== 0) {
    const log = outcome.stderr.trim().slice(-STDERR_TAIL_CHARS);
    throw new ComputerUseActionFailure(
      log || `xdotool ${args[0] ?? ""} exited with code ${outcome.code}`, log);
  }
  return outcome;
}

async function holdModifiers(
  toolchain: ComputerUseToolchain,
  deps: { environment: NodeJS.ProcessEnv; runCommand: ComputerUseRunCommand },
  modifierKeys: string | undefined,
): Promise<string[]> {
  if (!modifierKeys) return [];
  const normalized = normalizeXdotoolKeySequence(modifierKeys).split("+");
  for (const key of normalized) await xdotool(toolchain, deps, ["keydown", key]);
  return normalized;
}

async function releaseModifiers(
  toolchain: ComputerUseToolchain,
  deps: { environment: NodeJS.ProcessEnv; runCommand: ComputerUseRunCommand },
  held: string[],
): Promise<void> {
  for (const key of [...held].reverse()) {
    try {
      await xdotool(toolchain, deps, ["keyup", key]);
    } catch {}
  }
}

async function captureScreenshot(
  toolchain: ComputerUseToolchain,
  deps: { environment: NodeJS.ProcessEnv; runCommand: ComputerUseRunCommand },
): Promise<string> {
  const argv = toolchain.capturer === "import"
    ? ["import", "-window", "root", "webp:-"]
    : ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "x11grab",
       "-i", String(deps.environment.DISPLAY), "-frames:v", "1", "-f", "webp", "-"];
  let outcome: ComputerUseCommandOutcome;
  try {
    outcome = await deps.runCommand(argv, deps.environment);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new ComputerUseActionFailure(`screenshot capture failed to launch: ${message}`, message);
  }
  if (outcome.code !== 0 || !outcome.stdoutBytes.length) {
    const log = outcome.stderr.trim().slice(-STDERR_TAIL_CHARS);
    throw new ComputerUseActionFailure(
      log || `screenshot capture exited with code ${outcome.code}`, log);
  }
  return outcome.stdoutBytes.toString("base64");
}

async function cursorPosition(
  toolchain: ComputerUseToolchain,
  deps: { environment: NodeJS.ProcessEnv; runCommand: ComputerUseRunCommand },
): Promise<Coordinate> {
  const outcome = await xdotool(toolchain, deps, ["getmouselocation", "--shell"]);
  const values: Record<string, string> = {};
  for (const line of outcome.stdout.split("\n")) {
    const index = line.indexOf("=");
    if (index > 0) values[line.slice(0, index)] = line.slice(index + 1);
  }
  const x = Number.parseInt(values.X ?? "", 10);
  const y = Number.parseInt(values.Y ?? "", 10);
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    throw new ComputerUseActionFailure("cursor position unavailable", outcome.stdout);
  }
  return new Coordinate({ x, y });
}

export async function executeComputerUse(
  args: ComputerUseArgs,
  deps: ComputerUseDeps,
): Promise<ComputerUseResult> {
  const environment = deps.environment;
  const runCommand = deps.runCommand ?? defaultRunCommand;
  const sleep = deps.sleep ?? defaultSleep;
  const now = deps.now ?? (() => Date.now());
  const startedAt = now();
  const commandDeps = { environment, runCommand };

  const errorResult = (message: string, actionCount: number, log = "") =>
    new ComputerUseResult({
      result: {
        case: "error",
        value: new ComputerUseError({
          error: message,
          actionCount,
          durationMs: Math.max(0, Math.round(now() - startedAt)),
          ...(log ? { log } : {}),
        }),
      },
    });

  if (!String(environment.DISPLAY ?? "").trim()) {
    return errorResult("Computer use requires a DISPLAY; none is set in the runtime environment.", 0);
  }
  const toolchain = await resolveComputerUseToolchain(environment);
  if (!toolchain) {
    return errorResult(
      "Computer use is not supported here: xdotool and a screenshot tool (import or ffmpeg) must be resolvable.",
      0);
  }

  let completed = 0;
  const log: string[] = [];
  const run = async (xargs: string[]) => {
    const outcome = await xdotool(toolchain, commandDeps, xargs);
    if (outcome.stderr.trim()) log.push(outcome.stderr.trim());
    return outcome;
  };

  try {
    for (const action of args.actions) {
      switch (action.action.case) {
        case "mouseMove": {
          const coordinate = action.action.value.coordinate;
          if (!coordinate) throw new ComputerUseActionFailure("mouseMove requires a coordinate", "");
          await run(["mousemove", String(coordinate.x), String(coordinate.y)]);
          break;
        }
        case "click": {
          const value = action.action.value;
          const held = await holdModifiers(toolchain, commandDeps, value.modifierKeys);
          try {
            if (value.coordinate) {
              await run(["mousemove", String(value.coordinate.x), String(value.coordinate.y)]);
            }
            await run(["click", "--repeat", String(value.count || 1), String(buttonArg(value.button))]);
          } finally {
            await releaseModifiers(toolchain, commandDeps, held);
          }
          break;
        }
        case "mouseDown":
          await run(["mousedown", String(buttonArg(action.action.value.button))]);
          break;
        case "mouseUp":
          await run(["mouseup", String(buttonArg(action.action.value.button))]);
          break;
        case "drag": {
          const value = action.action.value;
          if (!value.path.length) throw new ComputerUseActionFailure("drag requires a non-empty path", "");
          const held = await holdModifiers(toolchain, commandDeps, value.modifierKeys);
          try {
            const start = value.path[0];
            const rest = value.path.slice(1);
            if (!start) throw new ComputerUseActionFailure("drag requires a non-empty path", "");
            await run(["mousemove", String(start.x), String(start.y)]);
            await run(["mousedown", String(buttonArg(value.button))]);
            try {
              for (const point of rest) {
                await run(["mousemove", String(point.x), String(point.y)]);
              }
            } finally {
              await run(["mouseup", String(buttonArg(value.button))]);
            }
          } finally {
            await releaseModifiers(toolchain, commandDeps, held);
          }
          break;
        }
        case "scroll": {
          const value = action.action.value;
          const held = await holdModifiers(toolchain, commandDeps, value.modifierKeys);
          try {
            if (value.coordinate) {
              await run(["mousemove", String(value.coordinate.x), String(value.coordinate.y)]);
            }
            await run(["click", "--repeat", String(value.amount || 1),
                       String(SCROLL_BUTTON_X11[value.direction] ?? 4)]);
          } finally {
            await releaseModifiers(toolchain, commandDeps, held);
          }
          break;
        }
        case "type":
          await run(["type", "--delay", "12", "--", action.action.value.text]);
          break;
        case "key": {
          const value = action.action.value;
          const sequence = normalizeXdotoolKeySequence(value.key);
          if (value.holdDurationMs != null) {
            const parts = sequence.split("+");
            for (const key of parts) await run(["keydown", key]);
            try {
              await sleep(Math.max(0, value.holdDurationMs));
            } finally {
              for (const key of [...parts].reverse()) await run(["keyup", key]);
            }
          } else {
            await run(["key", "--", sequence]);
          }
          break;
        }
        case "wait":
          await sleep(Math.min(Math.max(0, action.action.value.durationMs), WAIT_MAX_MS));
          break;
        case "screenshot":
          await captureScreenshot(toolchain, commandDeps);
          break;
        case "cursorPosition":
          await cursorPosition(toolchain, commandDeps);
          break;
        default:
          throw new ComputerUseActionFailure(`unsupported computer-use action: ${action.action.case ?? "unset"}`, "");
      }
      completed += 1;
    }
  } catch (error) {
    if (error instanceof ComputerUseActionFailure) {
      return errorResult(error.message, completed, error.log || log.join("\n"));
    }
    const message = error instanceof Error ? error.message : String(error);
    return errorResult(message, completed, log.join("\n"));
  }

  const lastCase = args.actions.at(-1)?.action.case;
  if (lastCase !== "screenshot" && lastCase !== "cursorPosition" && lastCase !== "wait") {
    await sleep(COMPUTER_USE_SCREENSHOT_SETTLE_DELAY_MS);
  }
  try {
    const screenshot = await captureScreenshot(toolchain, commandDeps);
    const position = await cursorPosition(toolchain, commandDeps);
    return new ComputerUseResult({
      result: {
        case: "success",
        value: new ComputerUseSuccess({
          actionCount: args.actions.length,
          durationMs: Math.max(0, Math.round(now() - startedAt)),
          screenshot,
          cursorPosition: position,
          ...(log.length ? { log: log.join("\n") } : {}),
        }),
      },
    });
  } catch (error) {
    if (error instanceof ComputerUseActionFailure) {
      return errorResult(error.message, completed, error.log || log.join("\n"));
    }
    const message = error instanceof Error ? error.message : String(error);
    return errorResult(message, completed, log.join("\n"));
  }
}
