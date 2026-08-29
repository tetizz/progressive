const version = new URL(import.meta.url).search;
const adapterUrl = new URL(`./wasm-kernel-adapter.js${version}`, import.meta.url);
const adapterModulePromise = import(adapterUrl.href);

let kernelPromise = null;
let pinnedSourceFingerprint = null;

function publicError(error, fallbackRequired = true) {
  return {
    code: String(error?.code || "browser-worker-failure"),
    message: String(error?.message || "The browser engine worker failed."),
    fallback_required: fallbackRequired,
  };
}

function notReadyError() {
  const error = new Error("The browser engine was not certified before use.");
  error.code = "browser-worker-not-ready";
  return error;
}

async function getKernel(expectedSourceFingerprint) {
  if (
    pinnedSourceFingerprint !== null
    && pinnedSourceFingerprint !== expectedSourceFingerprint
  ) {
    const error = new Error("The browser engine source identity changed.");
    error.code = "browser-worker-source-changed";
    throw error;
  }
  if (!kernelPromise) {
    pinnedSourceFingerprint = expectedSourceFingerprint;
    kernelPromise = adapterModulePromise.then(({ loadCertifiedBrowserKernel }) => (
      loadCertifiedBrowserKernel({ expectedSourceFingerprint })
    ));
  }
  return kernelPromise;
}

self.addEventListener("message", async (event) => {
  const message = event?.data;
  const id = message?.id;
  if (!Number.isInteger(id)) return;
  try {
    if (message.type === "probe") {
      const kernel = await getKernel(message.payload?.expected_source_fingerprint);
      self.postMessage({
        id,
        ok: true,
        payload: { ready: true, ...kernel.identity },
      });
      return;
    }
    if (message.type === "analyze") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = await kernel.analyze(message.payload);
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    if (message.type === "prefix") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = await kernel.inspectPrefix(message.payload);
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    if (message.type === "root-session-create") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = kernel.createRootSession(message.payload);
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    if (message.type === "root-safe-reselector-session-create") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = kernel.createRootSafetyReselectSession(message.payload);
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    if (message.type === "root-enumerate") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = kernel.enumerateRoot(message.payload);
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    if (message.type === "root-import") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = kernel.importRoot(message.payload);
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    if (message.type === "root-search") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = kernel.searchRootCandidate(message.payload);
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    if (message.type === "root-safety") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = kernel.probeRootSafety(message.payload);
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    if (message.type === "root-ladder") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = kernel.probeRootSingleReplyMateLadder(message.payload);
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    if (message.type === "root-terminal-mate") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = kernel.probeRootTerminalMate(message.payload);
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    if (message.type === "root-session-destroy") {
      if (!kernelPromise) throw notReadyError();
      const kernel = await kernelPromise;
      const result = kernel.destroyRootSession();
      self.postMessage({ id, ok: true, payload: result });
      return;
    }
    const error = new Error("Unknown browser engine worker message.");
    error.code = "browser-worker-message-invalid";
    throw error;
  } catch (error) {
    self.postMessage({ id, ok: false, error: publicError(error) });
  }
});
