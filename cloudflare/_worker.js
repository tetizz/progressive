// Source proposal only. Anonymous public API bridge; no runtime bindings/secrets.
const BACKEND = "https://progressive-ui9q.onrender.com";
const ROUTES = new Map([["/api/health", "GET"], ["/api/prefix", "POST"], ["/api/analyze", "POST"]]);
const REQUEST_MAX = 256 * 1024; // Same default admission bound as webapp.py.

function failure(status, code, message) {
  return new Response(JSON.stringify({ error: { code, message } }), {
    status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
  });
}

async function readBounded(stream, limit, signal) {
  if (signal.aborted) throw signal.reason;
  if (!stream) return new Uint8Array();
  const reader = stream.getReader();
  let rejectAbort;
  const aborted = new Promise((_, reject) => { rejectAbort = reject; });
  const onAbort = () => {
    void reader.cancel(signal.reason).catch(() => {});
    rejectAbort(signal.reason);
  };
  signal.addEventListener("abort", onAbort, { once: true });
  if (signal.aborted) onAbort();
  const chunks = []; let size = 0;
  try {
    while (true) {
      const { done, value } = await Promise.race([reader.read(), aborted]);
      if (signal.aborted) throw signal.reason;
      if (done) break;
      size += value.byteLength;
      if (size > limit) { const error = new Error("body limit exceeded"); error.code = "body-limit"; throw error; }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return bytes;
  } catch (error) {
    void reader.cancel(error).catch(() => {});
    throw error;
  } finally {
    signal.removeEventListener("abort", onAbort);
    reader.releaseLock();
  }
}

function publicHeaders() { return { "Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" }; }

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(request);
    const method = ROUTES.get(url.pathname);
    if (!method || url.search || url.hash) return failure(404, "api-route-not-found", "Public API route is not available.");
    if (request.method !== method) return failure(405, "api-method-not-allowed", "Method is not available for this API route.");
    const origin = request.headers.get("Origin");
    if ((origin !== null && origin !== url.origin) || (method === "POST" && origin !== url.origin)
        || ["cross-site", "same-site"].includes(request.headers.get("Sec-Fetch-Site"))) {
      return failure(403, "invalid-origin", "API requests must come from this exact origin.");
    }
    if (method === "POST" && (request.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase() !== "application/json") {
      return failure(415, "unsupported-media-type", "Content-Type must be application/json.");
    }
    const length = request.headers.get("Content-Length");
    if (length !== null && (!/^\d+$/.test(length) || Number(length) > REQUEST_MAX)) return failure(413, "request-too-large", "Request exceeds the public API body limit.");
    const controller = new AbortController(); let phase = "request";
    let downstream = null, finishStream = null;
    const detach = () => request.signal.removeEventListener("abort", onAbort);
    const onAbort = () => {
      const reason = new Error("client disconnected");
      controller.abort(reason);
      if (downstream) { try { downstream.error(reason); } catch {} }
      if (finishStream) void finishStream(reason, true);
    };
    request.signal.addEventListener("abort", onAbort, { once: true });
    if (request.signal.aborted) onAbort();
    try {
      const body = method === "POST" ? await readBounded(request.body, REQUEST_MAX, controller.signal) : null;
      if (controller.signal.aborted) throw controller.signal.reason;
      phase = "upstream";
      // Build a new header set: Origin, Cookie, Authorization, Referer and all
      // caller-controlled routing/forwarding headers never cross this boundary.
      const headers = { Accept: "application/json" };
      if (body !== null) { headers["Content-Type"] = "application/json"; headers["Content-Length"] = String(body.byteLength); }
      const upstream = await fetch(`${BACKEND}${url.pathname}`, {
        method, headers, body, redirect: "manual", signal: controller.signal,
        credentials: "omit", cache: "no-store",
      });
      if (upstream.status >= 300 && upstream.status < 400) {
        await upstream.body?.cancel();
        detach();
        return failure(502, "upstream-redirect-rejected", "The configured API returned a redirect.");
      }
      if (!(upstream.headers.get("Content-Type") || "").toLowerCase().startsWith("application/json")) {
        await upstream.body?.cancel();
        detach();
        return failure(502, "upstream-content-type", "The configured API did not return JSON.");
      }
      phase = "response";
      if (controller.signal.aborted) throw controller.signal.reason;
      // Keep one upstream read per downstream demand. No whole-body buffering,
      // response byte cap, new deadline, or eager read loop is introduced.
      if (!upstream.body) {
        detach();
        return new Response(null, { status: upstream.status, headers: publicHeaders() });
      }
      const reader = upstream.body.getReader(); let finished = false;
      finishStream = async (reason, cancelReader = false) => {
        if (finished) return;
        finished = true;
        detach();
        try { if (cancelReader) await reader.cancel(reason); }
        catch {} // Preserve the original transport failure/cancellation.
        finally { reader.releaseLock(); }
      };
      const bodyStream = new ReadableStream({
        start(streamController) { downstream = streamController; },
        async pull(streamController) {
          if (finished) return;
          try {
            const chunk = await reader.read();
            if (finished) return;
            if (controller.signal.aborted) throw controller.signal.reason;
            if (chunk.done) { streamController.close(); await finishStream(); }
            else streamController.enqueue(chunk.value);
          } catch (error) {
            if (!finished) { streamController.error(error); controller.abort(error); await finishStream(error, true); }
          }
        },
        async cancel(reason) { controller.abort(reason); await finishStream(reason, true); },
      }, { highWaterMark: 0 });
      if (controller.signal.aborted) onAbort();
      return new Response(bodyStream, { status: upstream.status, headers: publicHeaders() });
    } catch (error) {
      controller.abort(error);
      detach();
      if (request.signal.aborted) return failure(499, "client-disconnected", "The caller canceled this request.");
      if (error?.code === "body-limit") return failure(phase === "request" ? 413 : 502, "api-body-limit", "The public API body limit was exceeded.");
      return failure(502, "api-upstream-unavailable", "The configured public API is unavailable.");
    }
  },
};
