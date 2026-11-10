// Loaded only on the public site (public.py). Shows who's signed in, offers Sign
// out, and explains failures: when a login expires, Cloudflare redirects the page's
// data requests to its login site and the browser blocks them, which otherwise
// looks like "Can't reach the Mac".
(() => {
  const realFetch = window.fetch.bind(window);
  let checking = false;

  function banner(text, button) {
    let el = document.getElementById("publicBanner");
    if (!el) {
      el = document.createElement("div");
      el.id = "publicBanner";
      el.setAttribute("role", "alert");
      el.style.cssText = "background:var(--warn-bg);color:var(--warn-text);padding:10px 16px;display:flex;gap:12px;align-items:center;justify-content:center;flex-wrap:wrap;";
      document.body.prepend(el);
    }
    el.textContent = text;
    if (button) {
      const b = document.createElement("button");
      b.className = "btn";
      b.textContent = button;
      b.addEventListener("click", () => location.reload());
      el.append(b);
    }
  }

  async function diagnose() {
    if (checking) return;
    checking = true;
    try {
      const r = await realFetch("/api/whoami", { redirect: "manual", cache: "no-store" });
      if (r.type === "opaqueredirect" || r.status === 401) banner("Your sign-in has expired.", "Sign in again");
      else if (r.status === 403) banner("This account doesn't have access.");
      else if (r.status >= 500) banner("The site's computer isn't reachable right now. Anything on screen may be out of date.");
    } catch {
      banner("Can't connect right now. Anything on screen may be out of date.");
    } finally {
      checking = false;
    }
  }

  window.fetch = async (input, init) => {
    try {
      const r = await realFetch(input, init);
      if (r.status === 401 || r.status >= 500) diagnose();
      return r;
    } catch (err) {
      diagnose();
      throw err;
    }
  };

  document.addEventListener("DOMContentLoaded", async () => {
    const bar = document.querySelector("header .bar");
    if (!bar) return;
    const out = document.createElement("a");
    out.className = "nav";
    out.href = "/cdn-cgi/access/logout";
    out.textContent = "Sign out";
    bar.append(out);
    try {
      const me = await (await realFetch("/api/whoami", { cache: "no-store" })).json();
      if (me.email) out.title = "Signed in as " + me.email;
    } catch {}
  });
})();
