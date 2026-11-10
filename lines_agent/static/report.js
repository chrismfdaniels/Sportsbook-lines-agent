// Reports this app's failures to the Mac, which writes them to its log
// (~/Library/Logs/SportsbookLines/errors.log, lines starting PHONE).
// Sends nothing while everything works. A report waits on the phone only while
// the Mac is out of reach (at most 10, a few KB in all) and is deleted as soon
// as the Mac confirms it has it. Repeats of the same problem are counted, not resent.
(() => {
  const KEY = "report.queue", MAX_QUEUE = 10, MAX_MSG = 300, MAX_PER_PAGE = 25, SLOW_MS = 8000, REPEAT_MS = 300000;
  const realFetch = window.fetch.bind(window);
  const page = location.pathname;
  const app = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const recent = new Map();  // problem -> {e, at}
  let queue = read(), made = 0, timer = null, sending = false;

  function read() {
    try { const q = JSON.parse(localStorage.getItem(KEY) || "[]"); return Array.isArray(q) ? q.slice(-MAX_QUEUE) : []; }
    catch { return []; }
  }
  function write() {
    try { queue.length ? localStorage.setItem(KEY, JSON.stringify(queue)) : localStorage.removeItem(KEY); } catch {}
  }

  function report(kind, msg, extra = {}) {
    msg = String(msg || "").slice(0, MAX_MSG);
    const sig = kind + "|" + msg.replace(/\d+ ms/g, "");
    const seen = recent.get(sig);
    if (seen) {
      if (queue.includes(seen.e)) { seen.e.n = (seen.e.n || 1) + 1; write(); return; }  // not sent yet: count it
      if (Date.now() - seen.at < REPEAT_MS) { seen.skipped = (seen.skipped || 0) + 1; return; }
    }
    if (made >= MAX_PER_PAGE) return;
    made++;
    const e = { id: Math.random().toString(36).slice(2, 12), t: new Date().toISOString(), kind, page, msg, ...extra };
    if (seen && seen.skipped) e.n = seen.skipped + 1;
    recent.set(sig, { e, at: Date.now() });
    queue.push(e);
    if (queue.length > MAX_QUEUE) queue = queue.slice(-MAX_QUEUE);
    write();
    soon();
  }
  window.reportProblem = report;  // for a page's own caught errors

  function soon(ms = 1500) { clearTimeout(timer); timer = setTimeout(flush, ms); }
  async function flush() {
    if (sending || !queue.length) return;
    sending = true;
    const batch = queue.slice(0, MAX_QUEUE);
    try {
      const r = await realFetch("/api/log", { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify({ app, events: batch }), keepalive: true });
      if (r.ok) {
        const sent = new Set(batch.map((e) => e.id));
        queue = queue.filter((e) => !sent.has(e.id));
        write();
      }
    } catch {}  // still out of reach: keep them for next time
    finally { sending = false; }
  }

  // Every request the pages make: note failures, server errors and slow answers.
  window.fetch = async (input, init = {}) => {
    const url = new URL(input instanceof Request ? input.url : String(input), location.href);
    const what = `${(init.method || (input && input.method) || "GET").toUpperCase()} ${(url.pathname + url.search).slice(0, 120)}`;
    const t0 = performance.now();
    try {
      const r = await realFetch(input, init);
      const ms = Math.round(performance.now() - t0);
      if (r.status >= 400 && r.status !== 409) {  // 409: a scan is already running, not a fault
        let why = "";
        try { why = (await r.clone().json()).error || ""; } catch {}
        report("http", `${what} -> ${r.status}${why ? ": " + why : ""}`, { ms });
      } else if (ms > SLOW_MS && !url.searchParams.has("refresh") && url.pathname !== "/api/scan") {
        // Re-pulls and scans are meant to take a while; anything else this slow is a problem
        report("slow", `${what} was slow (the page may have shown a saved copy)`, { ms });
      } else if (queue.length) {
        soon(500);  // the Mac is answering again: send what's waiting
      }
      return r;
    } catch (err) {
      report("network", `${what}: ${(err && err.message) || err}`, { ms: Math.round(performance.now() - t0), online: navigator.onLine });
      throw err;
    }
  };

  addEventListener("error", (ev) => {
    const t = ev.target;
    if (t && t !== window && (t.src || t.href)) {
      // Cloudflare adds its analytics script to pages; the public site deliberately blocks it
      if ((t.src || "").includes("cloudflareinsights.com")) return;
      return report("resource", `couldn't load ${t.src || t.href}`);
    }
    report("script", `${ev.message} (${(ev.filename || "").split("/").pop()}:${ev.lineno}:${ev.colno})`);
  }, true);
  addEventListener("unhandledrejection", (ev) => {
    const r = ev.reason;
    report("promise", (r && (r.stack || r.message)) || String(r));
  });
  addEventListener("online", () => soon(500));
  addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && queue.length) soon(1000); });
  if (queue.length) soon(2000);  // left over from a time the Mac was out of reach
})();
