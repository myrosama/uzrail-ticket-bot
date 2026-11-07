/* UzRail Bot desktop window: talks to the local API of the bot running on this PC. */
(() => {
  "use strict";
  const KEY = new URLSearchParams(location.hash.slice(1)).get("k") || "";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  const CLASSES = {
    "3П": "Plaskart", "2К": "Kupe", "1Л": "SV / Lux", "2Е": "Sitting economy",
    "1С": "Sitting business", "2В": "Sitting 2nd class", "1В": "Sitting 1st class",
  };
  const cls = (c) => CLASSES[c] || c;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDate = (d) => { const [y, m, day] = String(d).split("-"); return y && m ? `${+day} ${MONTHS[+m - 1]}` : d; };
  const fmtSum = (n) => Math.round(+n || 0).toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  const fmtLeft = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  async function api(path, body) {
    const r = await fetch(path, {
      method: "POST", headers: { "X-App-Key": KEY, "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r.json();
  }

  let toastTimer;
  function toast(text) {
    const t = $("#toast");
    t.textContent = text; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 3200);
  }

  /* ---------------------------------------------------------------- navigation */
  let view = "status";
  try { view = localStorage.getItem("view") || "status"; } catch (e) { /* storage off */ }
  function show(v) {
    view = v;
    try { localStorage.setItem("view", v); } catch (e) { /* storage off */ }
    $$(".nav").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
    $$(".page").forEach((p) => (p.hidden = p.dataset.page !== v));
    if (v === "activity") renderFeed();
  }
  $$(".nav").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

  document.addEventListener("click", (e) => {
    const o = e.target.closest("[data-open]");
    if (o) api("/api/open", { target: "url", url: o.dataset.open });
    const a = e.target.closest("[data-act]");
    if (a) api("/api/open", { target: a.dataset.act });
  });

  /* ---------------------------------------------------------------- status */
  let S = null;
  let fetchedAt = 0;

  const STATES = {
    watching: { tone: "ok", live: true, title: "Watching for tickets" },
    holding: { tone: "warn", live: true, title: "Seats are held for you" },
    paused: { tone: "", title: "Paused" },
    idle: { tone: "", title: "No active trips" },
    setup: { tone: "warn", title: "Almost ready" },
    starting: { tone: "", title: "Starting…" },
    maintenance: { tone: "warn", title: "Railway site is unavailable" },
    error: { tone: "bad", title: "Something is wrong" },
  };

  function subline(s) {
    const active = s.trips.filter((t) => t.active);
    if (s.state === "watching") {
      const dates = new Set(active.flatMap((t) => t.dates));
      return `${active.length} trip${active.length === 1 ? "" : "s"} · ${dates.size} date${dates.size === 1 ? "" : "s"} · checking about every second`;
    }
    if (s.state === "paused") return "Nothing is checked until you resume.";
    if (s.state === "idle") return "Open the Telegram bot and add a trip in 🚆 Trips & settings.";
    if (s.state === "setup") return s.account ? s.detail : "Connect your railway account in the Telegram bot (🚆 Trips & settings → Account).";
    return s.detail || "";
  }

  function renderStatus() {
    const s = S;
    const key = s.holds.length ? "holding" : s.paused ? "paused" : s.state;
    const st = STATES[key] || { tone: "", title: key };
    const orb = $("#orb");
    orb.className = `orb ${st.tone} ${st.live ? "live" : ""}`;
    $("#headline").textContent = st.title;
    $("#subline").textContent = s.holds.length ? "Pay before the timer runs out, or the railway releases them." : subline(s);
    const age = s.lastCheckAge == null ? null : s.lastCheckAge + (Date.now() - fetchedAt) / 1000;
    $("#stAge").textContent = age == null ? "–" : age < 60 ? `${age.toFixed(1)} s ago` : `${Math.round(age / 60)} min ago`;
    $("#stRate").textContent = s.checksPerMin ? s.checksPerMin : "–";
    $("#stMs").textContent = s.medianMs ? `${s.medianMs} ms` : "–";
    const pb = $("#pauseBtn");
    pb.innerHTML = s.paused ? '<svg><use href="#i-play"/></svg><span>Resume</span>' : '<svg><use href="#i-pause"/></svg><span>Pause</span>';
    renderHolds();
  }

  let holdsSig = "";
  function renderHolds() {
    const el = $("#holds");
    const sig = JSON.stringify(S.holds.map((h) => [h.order, h.seats, h.pay_url, h.renewals]));
    if (sig !== holdsSig) {          // rebuild only when the holds change, so buttons stay clickable
      holdsSig = sig;
      el.innerHTML = S.holds.map((h) => `<div class="card hold">
        <div class="hold-top">
          <div><div class="hold-title">${esc(h.from)} → ${esc(h.to)}</div>
            <div class="muted small">${esc(fmtDate(h.date))} · train ${esc(h.train)} · ${esc(cls(h.cls))}</div></div>
          <div class="timer" data-order="${esc(h.order)}"></div>
        </div>
        <div class="kv">
          <div><span>Car</span><b>${esc(h.car)}</b></div>
          <div><span>Seats</span><b>${esc((h.seats || []).join(", ") || "–")}</b></div>
          <div><span>Passengers</span><b>${esc((h.names || []).join(", "))}</b></div>
          <div><span>Total</span><b>${fmtSum(h.total)} so'm</b></div>
        </div>
        <div class="row-btns">
          <button class="btn danger sm" data-cancel="${esc(h.order)}">Cancel</button>
          ${h.pay_url ? `<button class="btn primary sm" data-open="${esc(h.pay_url)}">Pay now</button>` : ""}
        </div>
      </div>`).join("");
    }
    tickTimers();
  }
  function tickTimers() {
    const elapsed = (Date.now() - fetchedAt) / 1000;
    for (const h of S.holds) {
      const t = $(`.timer[data-order="${CSS.escape(h.order)}"]`);
      if (t) t.textContent = fmtLeft(Math.max(0, h.left - elapsed));
    }
  }
  $("#holds").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-cancel]");
    if (!b || !confirm("Cancel this reservation? The seats go back on sale and the trip is paused.")) return;
    const r = await api("/api/cancel", { order: b.dataset.cancel });
    toast(r.ok ? "Reservation cancelled" : "Could not cancel. Try from Telegram.");
  });

  $("#pauseBtn").addEventListener("click", async () => {
    await api("/api/pause", { paused: !S.paused });
    toast(S.paused ? "Watching again" : "Paused");
    refresh();
  });

  /* ---------------------------------------------------------------- trips */
  let tripsSig = "";
  function renderTrips() {
    const sig = JSON.stringify(S.trips);
    if (sig === tripsSig) return;
    tripsSig = sig;
    const el = $("#trips");
    if (!S.trips.length) {
      el.innerHTML = `<div class="card empty-card"><p class="empty">No trips yet.</p>
        <button class="btn primary" data-act="telegram">Add a trip in Telegram</button></div>`;
      return;
    }
    el.innerHTML = S.trips.map((t) => {
      const dates = [...t.dates].sort();
      const shown = dates.slice(0, 6).map(fmtDate).join(", ") + (dates.length > 6 ? ` +${dates.length - 6}` : "");
      return `<div class="card trip ${t.active ? "" : "off"}">
        <div class="trip-main">
          <div class="trip-route">${esc(t.from.name)} <span class="arrow">→</span> ${esc(t.to.name)}</div>
          <div class="muted small">${esc(shown)}</div>
          <div class="tags">
            <span class="tag g">${t.mode === "buy" ? "Auto-buy" : "Notify only"}</span>
            ${t.classes.map((c) => `<span class="tag">${esc(cls(c))}</span>`).join("")}
            ${t.maxPrice ? `<span class="tag">≤ ${fmtSum(t.maxPrice)} so'm</span>` : ""}
            ${t.mode === "buy" ? `<span class="tag">${t.passengers.length} passenger${t.passengers.length === 1 ? "" : "s"}</span>` : ""}
            ${t.train ? `<span class="tag">train ${esc(t.train)}</span>` : ""}
          </div>
        </div>
        <label class="switch" title="${t.active ? "Pause this trip" : "Resume this trip"}">
          <input type="checkbox" data-trip="${esc(t.id)}" ${t.active ? "checked" : ""}><span></span></label>
      </div>`;
    }).join("");
  }
  $("#trips").addEventListener("change", async (e) => {
    const i = e.target.closest("[data-trip]");
    if (!i) return;
    await api("/api/trip", { id: i.dataset.trip, active: i.checked });
    toast(i.checked ? "Trip resumed" : "Trip paused");
    refresh();
  });

  /* ---------------------------------------------------------------- activity */
  const rows = [];
  let lastTs = 0;
  const EV = {
    session_start: null, heartbeat: null, notify: null, plan: null, miniapp: null, http: null, ws: null, check: null,
    start: (o) => [o.updated_from ? "ok" : "", o.updated_from ? `Updated v${o.updated_from} → v${o.version} and restarted` : `Bot v${o.version} started on this PC`],
    stop: () => ["", "Bot stopped"],
    state: (o) => (o.state === "watching" ? null : ["", `Status: ${o.state}${o.detail ? ` (${o.detail})` : ""}`]),
    availability: (o) => o.free > 0
      ? ["ok", `${o.free} seat${o.free === 1 ? "" : "s"} · ${cls(o.cls)} · train ${o.train} · ${fmtDate(o.date)} · ${fmtSum(o.price)} so'm`]
      : ["", `Seats sold out again on train ${o.train} (${cls(o.cls)})`],
    detect: (o) => ["ok", `New seats on ${fmtDate(o.date)}, booking now`],
    fanout: (o) => ["", `Trying ${o.cars.length} cars at once on train ${o.train}`],
    reserve_sent: (o) => ["", `Reserve sent for car ${o.car} (${o.ms} ms)`],
    reserve_refused: (o) => ["warn", `Railway refused car ${o.car}: ${o.message || o.status}`],
    settled: (o) => [o.result === "RESERVED" ? "ok" : "warn", `Car ${o.car || "?"}: ${o.result === "RESERVED" ? "reserved" : o.result.toLowerCase()}${o.ms ? ` after ${o.ms} ms` : ""}`],
    held: (o) => ["ok", `Held seats ${(o.seats || []).join(", ")} on train ${o.train}, car ${o.car} in ${o.took_s} s`],
    extra_hold_cancelled: (o) => ["", `Released a duplicate hold in car ${o.car}`],
    seats_gone: () => ["warn", "The seats were gone before we could book"],
    missed: (o) => ["bad", `Missed: ${o.why}`],
    pay_link: () => ["", "Payment link ready"],
    pay_link_fail: () => ["warn", "Could not make the payment link yet, retrying"],
    renew_attempt: (o) => [o.result === "RESERVED" ? "ok" : "warn", o.result === "RESERVED" ? "Hold renewed for another 12 minutes" : `Renewal failed (${o.result})`],
    renew_error: () => ["warn", "Renewal failed"],
    hold_end: (o) => ["warn", `Hold ended: ${o.why}`],
    recover_hold: () => ["", "Restored a held reservation after restart"],
    recover_cancel: () => ["", "Cancelled a leftover reservation from the last run"],
    recover_drop_hold: () => ["", "A saved reservation is no longer active"],
    paid: (o) => ["ok", `Paid! ${o.pdf ? "Ticket PDF sent to Telegram" : "Ticket confirmed"}`],
    pdf_fail: () => ["warn", "Could not download the ticket PDF (get it on the railway site)"],
    returned_from_bank: () => ["", "Back from the bank, checking the payment"],
    user_pause: () => ["", "Paused"],
    user_resume: () => ["", "Resumed"],
    user_cancel_hold: () => ["", "You cancelled a reservation"],
    trip_saved: (o) => ["", `Trip saved: ${o.dates.length} date${o.dates.length === 1 ? "" : "s"}, ${o.classes.map(cls).join(", ")}`],
    account_saved: (o) => ["ok", `Railway account connected (${o.passengers} saved passenger${o.passengers === 1 ? "" : "s"})`],
    learned_cars: (o) => ["", `Learned the car numbers of train ${o.train}`],
    check_fail: (o) => ["warn", `Check failed: ${o.error}`],
    http_fail: (o) => ["warn", `Network problem${o.error ? `: ${o.error}` : ""}`],
    book_error: (o) => ["bad", `Booking error: ${o.error}`],
    cars_retry: () => ["", "Car list was empty, asking again"],
    update_found: (o) => ["", `New version v${o.latest} found (you have v${o.current})`],
    update_installed: (o) => ["ok", `v${o.version} installed`],
    update_restart: (o) => ["", `Restarting into v${o.new}`],
    update_failed: (o) => ["warn", `Update to v${o.version} failed: ${o.error}`],
  };

  function describe(r) {
    const f = EV[r.kind];
    if (f === null) return null;
    if (!f) return ["", r.kind.replace(/_/g, " ")];
    try { return f(r); } catch (e) { return ["", r.kind.replace(/_/g, " ")]; }
  }

  async function pullActivity() {
    try {
      const r = await api("/api/activity", { after: lastTs });
      for (const row of r.rows) {
        lastTs = Math.max(lastTs, row.ts);
        const d = describe(row);
        if (d) rows.push({ t: row.t, tone: d[0], text: d[1] });
      }
      if (rows.length > 400) rows.splice(0, rows.length - 400);
      if (r.rows.length && view === "activity") renderFeed();
    } catch (e) { /* the app may be closing */ }
  }

  function renderFeed() {
    const el = $("#feed");
    if (!rows.length) { el.innerHTML = '<p class="empty">Nothing yet.</p>'; return; }
    el.innerHTML = rows.slice().reverse().map((r) =>
      `<div class="ev ${r.tone}"><time>${esc(r.t.slice(0, 8))}</time><i></i><span>${esc(r.text)}</span></div>`).join("");
  }

  /* ---------------------------------------------------------------- updates */
  function updateText(u) {
    const v = u.latest ? `v${u.latest}` : "a new version";
    switch (u.state) {
      case "downloading": return [true, `Downloading ${v}… The bot keeps watching meanwhile.`];
      case "installing": return [true, `Installing ${v}… The bot keeps watching meanwhile.`];
      case "ready": return [true, `${v} is installed. Restarting as soon as no booking is in progress.`];
      case "restarting": return [true, `Restarting into ${v}…`];
      case "available": return [false, `${v} is available. Update it in Settings.`];
      default: return null;
    }
  }
  function renderUpdate() {
    const u = S.update || {};
    const b = $("#updBanner");
    const t = updateText(u);
    b.hidden = !t;
    if (t) b.innerHTML = (t[0] ? '<span class="spinner"></span>' : "") + `<span>${esc(t[1])}</span>`;
    const when = u.checked ? new Date(u.checked * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
    const line = {
      current: `Up to date${when ? ` (checked ${when})` : ""}`, checking: "Checking for updates…",
      available: `v${u.latest} available`, downloading: `Downloading v${u.latest}…`, installing: `Installing v${u.latest}…`,
      ready: `v${u.latest} installed, restarting soon`, restarting: "Restarting…", offline: "Couldn't reach GitHub; will try again",
      failed: `Update failed: ${u.error || "unknown error"}. Will try again`, dev: "Running from source (updates come from git)",
      idle: "",
    }[u.state] || "";
    $("#setVer").textContent = `UzRail Bot ${S.version}${line ? ` · ${line}` : ""}`;
    const btn = $("#updBtn");
    btn.textContent = u.state === "available" ? "Update now" : "Check now";
    btn.disabled = ["checking", "downloading", "installing", "ready", "restarting", "dev"].includes(u.state);
    $("#autoUpdate").checked = S.autoUpdate;
  }
  $("#updBtn").addEventListener("click", async () => {
    await api("/api/update", { install: (S.update || {}).state === "available" });
    toast("Checking for updates…");
  });
  $("#autoUpdate").addEventListener("change", async (e) => {
    const r = await api("/api/autoupdate", { on: e.target.checked });
    toast(r.on ? "Updates install automatically" : "You'll be told about updates; install them from here");
  });

  /* ---------------------------------------------------------------- settings */
  function renderSettings() {
    const s = S;
    $("#autostart").checked = s.autostart;
    $("#setBot").textContent = s.bot ? `@${s.bot}` : "–";
    $("#setOwner").textContent = s.owner || "–";
    const tg = $("#setTg");
    tg.className = `pill ${s.telegramOk === false ? "bad" : "ok"}`;
    tg.textContent = s.telegramOk === false ? "Not reachable" : "Connected";
    $("#setAcc").textContent = s.account || "Not connected yet";
    $("#setDir").textContent = s.dataDir;
  }
  $("#autostart").addEventListener("change", async (e) => {
    const r = await api("/api/autostart", { on: e.target.checked });
    toast(r.on ? "Starts automatically when you sign in" : "Autostart turned off");
  });
  $("#quitBtn").addEventListener("click", async () => {
    if (!confirm("Quit UzRail Bot? It stops watching for tickets until you open it again.")) return;
    await api("/api/quit");
    document.body.innerHTML = '<p class="empty">UzRail Bot has stopped. You can close this window.</p>';
  });

  /* ---------------------------------------------------------------- first-run setup */
  let setupStep = 0;
  let waitLoop = false;
  function setupShow(n) {
    setupStep = n;
    $$(".step").forEach((s) => (s.hidden = +s.dataset.step !== n));
    $$("#steps li").forEach((li, i) => li.classList.toggle("on", i < n));
  }

  $("#tokenBtn").addEventListener("click", async () => {
    const btn = $("#tokenBtn");
    const err = $("#tokenErr");
    err.hidden = true; btn.disabled = true; btn.textContent = "Checking…";
    try {
      const r = await api("/api/setup/token", { token: $("#token").value });
      if (!r.ok) throw new Error(r.error);
      $("#botName").textContent = `@${r.bot}`;
      $("#openBot").onclick = () => api("/api/open", { target: "url", url: `https://t.me/${r.bot}` });
      setupShow(2);
      waitForOwner();
    } catch (e) {
      err.textContent = e.message; err.hidden = false;
    } finally {
      btn.disabled = false; btn.textContent = "Continue";
    }
  });
  $("#token").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#tokenBtn").click(); });

  async function waitForOwner() {
    if (waitLoop) return;
    waitLoop = true;
    $("#waiting").hidden = false; $("#who").hidden = true; $("#whoBtns").hidden = true; $("#waitErr").hidden = true;
    while (setupStep === 2) {
      try {
        const r = await api("/api/setup/wait");
        if (!r.ok) throw new Error(r.error);
        if (r.owner) {
          $("#waiting").hidden = true;
          $("#who").hidden = false; $("#whoBtns").hidden = false;
          $("#whoAvatar").textContent = (r.owner.name || "?").trim().charAt(0).toUpperCase();
          $("#whoName").textContent = r.owner.name || "Telegram user";
          $("#whoSub").textContent = (r.owner.username ? `@${r.owner.username} · ` : "") + `id ${r.owner.id}`;
          break;
        }
      } catch (e) {
        $("#waitErr").textContent = `${e.message}. Retrying…`; $("#waitErr").hidden = false;
        await new Promise((ok) => setTimeout(ok, 3000));
      }
    }
    waitLoop = false;
  }
  $("#notMe").addEventListener("click", () => {
    toast("Send a message to the bot from your own Telegram account.");
    waitForOwner();
  });
  $("#itsMe").addEventListener("click", async () => {
    const r = await api("/api/setup/confirm");
    if (!r.ok) { toast(r.error); return; }
    setupShow(3);
    toast("Linked! Open the bot and tap 🚆 Trips & settings.");
    refresh();
  });

  /* ---------------------------------------------------------------- loop */
  async function refresh() {
    try {
      S = await api("/api/state");
      fetchedAt = Date.now();
    } catch (e) {
      $("#headline") && ($("#headline").textContent = "The bot is not running");
      return;
    }
    const configured = S.configured;
    if (!configured) {
      if ($("#setup").hidden) { $("#setup").hidden = false; $("#shell").hidden = true; setupShow(1); $("#token").focus(); }
      return;
    }
    if ($("#shell").hidden) { $("#setup").hidden = true; $("#shell").hidden = false; show(view); }
    renderStatus(); renderTrips(); renderSettings(); renderUpdate();
  }

  refresh();
  pullActivity();
  setInterval(refresh, 1000);
  setInterval(() => { if (S && S.holds.length) tickTimers(); }, 250);
  setInterval(pullActivity, 2000);
})();
