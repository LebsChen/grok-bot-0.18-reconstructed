// Minimal CDP screenshot: connect to <port>'s first page target,
// capture a PNG plus document title/URL. Node >=22 global WebSocket.
import { writeFile } from "node:fs/promises";

const [portArg = "9229", outPng = "window.png", outJson = "window.json"] = process.argv.slice(2);

async function jsonVersion() {
  const response = await fetch(`http://127.0.0.1:${portArg}/json/list`);
  if (!response.ok) throw new Error(`CDP /json/list HTTP ${response.status}`);
  return response.json();
}

const targets = await jsonVersion();
const page = targets.find(target => target.type === "page");
if (!page) throw new Error("no page target on the CDP endpoint");

const socket = new WebSocket(page.webSocketDebuggerUrl);
let counter = 0;
const pending = new Map();
function send(method, params = {}) {
  const id = ++counter;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});
socket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (message.id != null && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message));
    else resolve(message.result);
  }
});

await send("Page.enable");
await new Promise(resolve => setTimeout(resolve, 15000));
const { data } = await send("Page.captureScreenshot", { format: "png" });
await writeFile(outPng, Buffer.from(data, "base64"));
const { result } = await send("Runtime.evaluate", {
  expression: "JSON.stringify({title: document.title, href: location.href})",
  returnByValue: true,
});
await writeFile(outJson, `${result.value}\n`);
console.log(`captured ${outPng} + ${outJson}: ${result.value}`);
socket.close();
