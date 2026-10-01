#!/usr/bin/env node
// Run a script with GROK_BOT_TARGET set so darwin-arm64 remains the
// default everywhere else: `with-target.mjs win32-x64 <script> [args]`.
import { pathToFileURL } from "node:url";
import path from "node:path";

const [target, script, ...rest] = process.argv.slice(2);
if (!target || !script) {
  throw new Error("usage: with-target.mjs <darwin-arm64|win32-x64> <script> [args]");
}
process.env.GROK_BOT_TARGET = target;
process.argv = [process.argv[0], path.resolve(script), ...rest];
await import(pathToFileURL(path.resolve(script)).href);
