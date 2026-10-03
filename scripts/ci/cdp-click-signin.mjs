// CDP sign-in driver: connect to <port>'s first page target, click the
// renderer's Sign-in button if present, wait for the route to leave the
// sign-in view, then dump {url, heading, clicked, signedIn} as JSON.
// Node >=22 global WebSocket. Usage:
//   node cdp-click-signin.mjs <port> <out.json> [waitSecs]
import { writeFile } from "node:fs/promises";

const [portArg = "9229", outJson = "window-signed-in.json",
  waitSecs = "45"] = process.argv.slice(2);

const targets = await (await fetch(
  `http://127.0.0.1:${portArg}/json/list`)).json();
const page = targets.find(t => t.type === "page");
if (!page) throw new Error("no page target on the CDP endpoint");

const socket = new WebSocket(page.webSocketDebuggerUrl);
let counter = 0;
const pending = new Map();
const send = (method, params = {}) => {
  const id = ++counter;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => pending.set(id, { res, rej }));
};
await new Promise((res, rej) => {
  socket.addEventListener("open", res, { once: true });
  socket.addEventListener("error", rej, { once: true });
});
socket.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id != null && pending.has(m.id)) {
    const { res, rej } = pending.get(m.id);
    pending.delete(m.id);
    m.error ? rej(new Error(m.error.message)) : res(m.result);
  }
});

const evaluate = async expr => {
  const r = await send("Runtime.evaluate", {
    expression: expr, returnByValue: true, awaitPromise: true });
  return r.result?.value;
};

// The desktop bundles both "core domain" routes; click any element
// whose text is a sign-in CTA.
const clicked = await evaluate(`(() => {
  const els = [...document.querySelectorAll("button,a,[role=button]")];
  const hit = els.find(e => /sign\\s*in|log\\s*in|continue/i
    .test(e.textContent || "") && e.offsetParent !== null);
  if (hit) { hit.click(); return hit.textContent.trim().slice(0, 40); }
  return null;
})()`);

const deadline = Date.now() + Number(waitSecs) * 1000;
let snap = {};
while (Date.now() < deadline) {
  snap = await evaluate(`(() => {
    const body = document.body ? document.body.innerText : "";
    const h = document.querySelector("h1,h2,[role=heading]");
    return { url: location.href,
      heading: h ? h.innerText.trim().slice(0, 80) : "",
      onSignIn: /sign\\s*in|log\\s*in/i.test(h ? h.innerText : "") &&
        body.length < 4000 };
  })()`).catch(() => ({}));
  if (snap.url && !snap.onSignIn) break;
  await new Promise(r => setTimeout(r, 1500));
}
const out = { clicked, signedIn: !!(snap.url && !snap.onSignIn), ...snap };
await writeFile(outJson, JSON.stringify(out, null, 2));
console.log(JSON.stringify(out));
socket.close();
process.exit(out.signedIn ? 0 : 2);
