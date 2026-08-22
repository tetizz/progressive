const proofUrl = process.argv[2] ?? "http://127.0.0.1:8891/abi-proof.html";
const targets = await fetch("http://127.0.0.1:9235/json/list").then((response) => response.json());
const target = targets.find((item) => item.url?.includes("/abi-proof.html"))
  ?? targets.find((item) => item.type === "page" && item.url?.startsWith("http://127.0.0.1:"));
if (!target) throw new Error("an Opera local-proof tab was not found");

const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});
let nextId = 1;
const pending = new Map();
socket.addEventListener("message", (event) => {
  const payload = JSON.parse(event.data);
  if (!payload.id || !pending.has(payload.id)) return;
  const { resolve, reject } = pending.get(payload.id);
  pending.delete(payload.id);
  payload.error ? reject(new Error(payload.error.message)) : resolve(payload);
});
function command(method, params = {}) {
  const id = nextId++;
  const reply = new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  socket.send(JSON.stringify({ id, method, params }));
  return reply;
}

if (!target.url?.includes("/abi-proof.html")) {
  await command("Page.navigate", { url: proofUrl });
}

let body = "";
for (let attempt = 0; attempt < 100; attempt += 1) {
  const payload = await command("Runtime.evaluate", {
    expression: "document.body?.innerText ?? ''",
    returnByValue: true,
  });
  body = payload.result.result.value;
  if (body.includes('"schema": "spc-opera-abi-smoke-v1"')) break;
  await new Promise((resolve) => setTimeout(resolve, 100));
}
socket.close();
if (!body.includes('"schema": "spc-opera-abi-smoke-v1"')) {
  throw new Error("Opera ABI proof did not complete within 10 seconds");
}
console.log(body);
