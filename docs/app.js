/* UzRail Bot Mini App.
 * Opened from the bot's keyboard button. The bot puts the current state (trips, saved passengers,
 * account email, the PC's public key) in the link's #fragment; changes go back with
 * Telegram.WebApp.sendData (max 4096 bytes), which needs no server. */
(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  // Opened from a keyboard button (the only way sendData works), Telegram passes NO initData;
  // it does pass the platform (tdesktop, android, ios, weba...). Outside Telegram it is "unknown".
  const inTelegram = !!(tg && (tg.initData || (tg.platform && tg.platform !== "unknown")));

  const CLASSES = [
    { c: "3П", name: "Plaskart", sub: "open sleeper" },
    { c: "2К", name: "Kupe", sub: "4-berth compartment" },
    { c: "1Л", name: "SV", sub: "2-berth" },
    { c: "1А", name: "Lux", sub: "premium 2-berth" },
    { c: "2Е", name: "Sitting", sub: "economy" },
    { c: "1С", name: "Sitting", sub: "business" },
    { c: "2В", name: "Sitting", sub: "2nd class" },
    { c: "1В", name: "Sitting", sub: "1st class" },
  ];
  const SPEEDS = [
    { v: 1, label: "Normal", sub: "every 0.8 s" },
    { v: 2, label: "Fast", sub: "every 0.4 s" },
    { v: 4, label: "Max", sub: "every 0.2 s" },
  ];
  const MAX_PAX = 4;
  const MAX_DATES = 30;

  // ------------------------------------------------------------------ state from the bot
  function decodeState() {
    const m = location.hash.match(/[#&]s=([^&]+)/);
    if (!m) return null;
    try {
      const b64 = m[1].replace(/-/g, "+").replace(/_/g, "/");
      const bytes = Uint8Array.from(atob(b64), (ch) => ch.charCodeAt(0));
      return JSON.parse(new TextDecoder().decode(bytes));
    } catch (e) {
      return null;
    }
  }
  const S = decodeState() || { email: "", hasPw: false, key: "", pax: [], trips: [], demo: true };
  S.pax = S.pax || [];
  S.trips = S.trips || [];
  // S.v >= 2: the bot understands several changes in one save. An older bot (not updated yet) keeps the old way.
  const canBatch = S.v >= 2;
  const pending = [];       // unsaved changes on the home page: {a:"toggle",id,active} or {a:"del",id}
  const originalTrips = JSON.stringify(S.trips);
  const paxName = (id) => (S.pax.find((p) => p.i === id) || { n: "Passenger" }).n;

  // ------------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const app = $("#app");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const title = (s) => String(s || "").toLowerCase().replace(/(^|[\s-])\S/g, (m) => m.toUpperCase());
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const today = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d; };
  const fmtDay = (s) => new Date(s + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  const fmtSum = (n) => String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  function classMeta(c) {
    const k = CLASSES.find((x) => x.c === c);
    if (k) return k;
    const sit = /[СВЕ]$/.test(c);
    const name = sit ? "Sitting" : c[0] === "3" ? "Plaskart" : c[0] === "2" ? "Kupe" : c[0] === "1" ? "SV" : "Class";
    return { c, name, sub: `class ${c}` };
  }
  const classLabel = (c) => { const k = classMeta(c); return `${k.name} ${k.sub}`; };
  const shortClass = (c) => { const k = classMeta(c); return k.name === "Sitting" || !CLASSES.some((x) => x.c === c) ? `${k.name} ${k.sub.replace("class ", "")}` : k.name; };
  const haptic = () => { try { tg && tg.HapticFeedback && tg.HapticFeedback.selectionChanged(); } catch (e) { /* older clients */ } };
  const newer = (v) => inTelegram && tg.isVersionAtLeast && tg.isVersionAtLeast(v);
  // Ask before Telegram closes the app with unsaved edits (Bot API 6.2+).
  const closingConfirm = (on) => { if (newer("6.2")) { try { on ? tg.enableClosingConfirmation() : tg.disableClosingConfirmation(); } catch (e) { /* */ } } };
  // In the long editor a swipe down while scrolling would close the app (7.7+): allow it only on the home page.
  const swipes = (on) => { if (newer("7.7")) { try { on ? tg.enableVerticalSwipes() : tg.disableVerticalSwipes(); } catch (e) { /* */ } } };
  const refocus = (sel) => { const el = document.querySelector(sel); if (el) el.focus({ preventScroll: true }); return !!el; };
  const confirmThen = (msg, go) => { if (inTelegram && tg.showConfirm) tg.showConfirm(msg, (ok) => ok && go()); else if (confirm(msg)) go(); };

  function toast(msg, ms = 2600) {
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, ms);
  }

  /* Over the live link when connected (the page stays open; returns true), otherwise through the chat. */
  async function send(payload) {
    if (liveOn && payload.a !== "account") {
      try {
        const n = Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");
        const r = await fetch(liveUrl, { method: "POST", body: await seal({ ...payload, ts: Date.now() / 1000, n }) });
        if (r.ok) return true;
      } catch (e) { /* the chat below still works */ }
    }
    const data = JSON.stringify(payload);
    if (data.length > 4000) { toast("Too much data for one save. Remove some dates."); return; }
    if (inTelegram) { closingConfirm(false); tg.sendData(data); return; }   // closes the Mini App; the bot replies
    console.log("sendData", payload);
    toast("Preview mode: open this page from your bot to save.");
  }

  // ------------------------------------------------------------------ primary button (Telegram MainButton or our own)
  let primaryHandler = null;
  let lastPrimary = null;
  function setPrimary(text, handler, enabled = true) {
    primaryHandler = handler;
    lastPrimary = [text, handler, enabled];
    if (inTelegram) {
      const mb = tg.MainButton;
      const dark = document.documentElement.dataset.theme === "dark";
      mb.setParams({ text, color: dark ? "#2EDB7F" : "#087A41", text_color: dark ? "#04140B" : "#FFFFFF", is_active: enabled, is_visible: !!handler });
      return;
    }
    const bar = $("#bottomBar"), btn = $("#primaryBtn");
    bar.hidden = !handler;
    btn.textContent = text;
    btn.disabled = !enabled;
  }
  if (inTelegram) tg.MainButton.onClick(() => primaryHandler && primaryHandler());
  $("#primaryBtn").addEventListener("click", () => primaryHandler && primaryHandler());

  let backHandler = null;
  function setBack(handler) {
    backHandler = handler;
    if (inTelegram) { handler ? tg.BackButton.show() : tg.BackButton.hide(); }
    else { $("#back").hidden = !handler; }
  }
  if (inTelegram) tg.BackButton.onClick(() => backHandler && backHandler());
  $("#back").addEventListener("click", () => backHandler && backHandler());

  // ------------------------------------------------------------------ stations (bundled list; the railway API can't be called from here)
  let STATIONS = [];
  const norm = (s) => String(s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[ʻʼ'’`‘]/g, "").replace(/\s+/g, " ").trim();
  fetch("stations.json").then((r) => r.json()).then((list) => {
    STATIONS = list.map((s) => ({ ...s, key: [s.en, s.uz, s.ru].map(norm).join(" | ") }));
  }).catch(() => { STATIONS = []; });

  // ------------------------------------------------------------------ timetable (bundled; built by tools/build_timetable.py)
  let TT = null;
  let onTimetable = null;
  fetch("timetable.json?v=2").then((r) => r.json()).then((t) => { TT = t; if (onTimetable) onTimetable(); })
    .catch(() => { TT = { trains: {}, groups: {} }; if (onTimetable) onTimetable(); });
  const WEEKDAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
  const codesFor = (code) => new Set((TT && TT.groups && TT.groups[code]) || [code]);

  /* Trains that stop at `from` and later at `to`: number, brand, classes, dep/arr times, nights, running days. */
  function routeTrains(from, to) {
    if (!TT || !from || !to) return [];
    const A = codesFor(from), B = codesFor(to), out = [];
    for (const [n, t] of Object.entries(TT.trains || {})) {
      const s = t.s || [];
      const i = s.findIndex((x) => A.has(x[0]) && x[2]);
      if (i < 0) continue;
      let j = -1;
      for (let k = i + 1; k < s.length; k++) if (B.has(s[k][0])) { j = k; break; }
      if (j < 0) continue;
      out.push({ n, b: t.b || "", c: t.c || [], dep: s[i][2], arr: s[j][1] || s[j][2],
                 nights: Math.max(0, s[j][3] - s[i][4]), fromDay: s[i][4], r: t.r || null, w: t.w || null });
    }
    return out.sort((x, y) => x.dep.localeCompare(y.dep));
  }

  /* The timetable's day names come in English or Russian. */
  const DAY_KEYS = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6,
                     "вс": 0, "пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6 };
  const DAY_EN = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const dayIndex = (x) => { const k = String(x).toLowerCase().slice(0, 3); return DAY_KEYS[k] ?? DAY_KEYS[k.slice(0, 2)]; };
  const isDaily = (x) => /every|daily|ежедневно/i.test(String(x));

  /* Does the train leave `from` on this date? true / false / null (the timetable doesn't say). */
  function runsOn(t, isoDate) {
    if (Array.isArray(t.w) && t.w.length) {                     // weekdays seen in the railway's own lists
      const o = new Date(isoDate + "T00:00:00");
      o.setDate(o.getDate() - (t.fromDay || 0));
      return t.w.includes(o.getDay());
    }
    const r = t.r;
    if (!r) return null;
    const dt = new Date(isoDate + "T00:00:00");
    dt.setDate(dt.getDate() - (t.fromDay || 0));                 // the date it left its first station
    const dd = String(dt.getDate()).padStart(2, "0"), mm = String(dt.getMonth() + 1).padStart(2, "0");
    const md = (x) => { const m = String(x || "").match(/^(\d{2})\.(\d{2})/); return m ? m[2] + m[1] : null; };
    const cur = mm + dd;
    // outside this timetable season: the next season usually continues the train, so "don't know"
    if (r.from && md(r.from) && cur < md(r.from) && !(md(r.from) > "1100" && cur < "0300")) return null;
    if (r.to && md(r.to) && cur > md(r.to) && !(md(r.to) < "0300" && cur > "1100")) return null;
    if (Array.isArray(r.dates) && r.dates.length) return r.dates.some((x) => String(x).startsWith(`${dd}.${mm}`));
    const days = (r.daysOfWeek || []).map(String);
    if (!days.length || days.some(isDaily)) return true;
    if (days.some((x) => /odd|нечет/i.test(x))) return dt.getDate() % 2 === 1;
    if (days.some((x) => /even|чет/i.test(x))) return dt.getDate() % 2 === 0;
    const idx = days.map(dayIndex).filter((x) => x !== undefined);
    return idx.length ? idx.includes(dt.getDay()) : null;
  }
  const runText = (r, w) => {
    if (Array.isArray(w) && w.length) return w.length >= 7 ? "" : w.map((i) => DAY_EN[i]).join(", ");
    if (!r) return "";
    const days = (r.daysOfWeek || []).map(String);
    if (Array.isArray(r.dates) && r.dates.length) return "on set dates";
    if (!days.length || days.some(isDaily)) return "";
    if (days.some((x) => /odd|нечет/i.test(x))) return "odd dates";
    if (days.some((x) => /even|чет/i.test(x))) return "even dates";
    const idx = days.map(dayIndex).filter((x) => x !== undefined);
    return idx.length ? idx.map((i) => DAY_EN[i]).join(", ") : "";
  };

  function searchStations(q) {
    const n = norm(q);
    if (n.length < 2) return [];
    const scored = [];
    for (const s of STATIONS) {
      const names = [s.en, s.uz, s.ru].map(norm);
      let score = -1;
      if (names.some((x) => x.startsWith(n))) score = 2;
      else if (names.some((x) => x.split(/[\s-]/).some((w) => w.startsWith(n)))) score = 1;
      else if (s.key.includes(n)) score = 0;
      if (score >= 0) scored.push([score, s.uzb ? 1 : 0, s.t || 0, s]);
    }
    scored.sort((a, b) => b[0] - a[0] || b[1] - a[1] || b[2] - a[2]);
    return scored.slice(0, 8).map((x) => x[3]);
  }

  // ------------------------------------------------------------------ views
  function render(view, arg) {
    window.scrollTo(0, 0);
    app.onclick = app.oninput = app.onchange = null;      // the editor's unsaved-changes watcher
    closingConfirm(false);
    swipes(view === "home");
    currentView = view;
    const home = view === "home";
    $("#tabs").hidden = !home;
    document.body.classList.toggle("has-tabs", home);
    delete $("#subtitle").dataset.tone;
    if (home) return renderTab();
    if (view === "trip") return renderTrip(arg);
    if (view === "account") return renderAccount();
  }

  // ------------------------------------------------------------------ live link to the PC (uzrailbot/remote.py)
  const liveUrl = S.live ? S.live.u + S.live.i : "";
  let liveOn = false, liveAge = 0, gotAt = 0, liveKey = null, shownSig = "", currentView = "";
  let tab = "now", tripsTab = "active", feedFilter = "all", sheet = null, shownHolds = 0;
  const bytes = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  async function keyOf() {
    if (!liveKey) liveKey = await crypto.subtle.importKey("raw", bytes(S.live.k.replace(/-/g, "+").replace(/_/g, "/")), "AES-GCM", false, ["encrypt", "decrypt"]);
    return liveKey;
  }
  async function seal(obj) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await keyOf(), new TextEncoder().encode(JSON.stringify(obj))));
    const all = new Uint8Array(iv.length + ct.length);
    all.set(iv); all.set(ct, iv.length);
    let s = "";
    for (const b of all) s += String.fromCharCode(b);
    return btoa(s);
  }
  async function unseal(text) {
    const raw = bytes(text);
    return JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({ name: "AES-GCM", iv: raw.slice(0, 12) }, await keyOf(), raw.slice(12))));
  }
  async function pull() {
    try {
      const r = await fetch(liveUrl, { cache: "no-store" });
      if (r.status !== 200) { liveOn = false; refreshShown(); return; }
      const j = await r.json();
      Object.assign(S, await unseal(j.data));
      liveAge = j.age; gotAt = Date.now(); liveOn = true;
      S.pax = S.pax || []; S.trips = S.trips || [];
      refreshShown();
    } catch (e) { liveOn = false; refreshShown(); }
  }
  const pcLost = () => !!(S.live && gotAt && (!liveOn || elapsed() > 35));
  const stateSig = () => JSON.stringify([S.holds, S.trips, S.paused, S.status && [S.status.state, S.status.detail], S.targets, S.shareLogs, S.autoUpdate,
    (S.board || []).map((b) => [b.from, b.to, b.date, b.trains]), (S.activity || []).length && S.activity[S.activity.length - 1].ts, liveOn, pcLost()]);
  function refreshShown() {
    if (currentView !== "home" || sheet || (document.activeElement && document.activeElement.tagName === "TEXTAREA")) return;
    if (stateSig() !== shownSig) renderTab();
  }

  // ------------------------------------------------------------------ small pieces
  const ms = (n, c = "") => `<span class="ms ${c}" aria-hidden="true">${n}</span>`;
  const hhmm = (x) => String(x || "").slice(-5);
  const nextDay = (dep, arr) => arr && String(dep || "").slice(0, 10) !== String(arr).slice(0, 10);
  const ago = (sec) => sec < 60 ? `${Math.max(1, Math.round(sec))} s ago` : `${Math.round(sec / 60)} min ago`;
  const elapsed = () => (Date.now() - gotAt + liveAge) / 1000;
  const dayLong = (s) => new Date(s + "T00:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }).replace(",", "");
  const clock = (left) => `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  const secsLeft = (exp) => Math.floor(+exp - Date.now() / 1000);
  const activeHolds = () => (S.holds || []).filter((h) => h.exp * 1000 > Date.now()).sort((a, b) => a.exp - b.exp);
  const guardPending = () => { if (pending.length) { toast("Save your changes first"); return false; } return true; };
  const isCompleted = (t) => (t.dates || []).every((d) => d < iso(today())) ||
    (t.mode === "buy" && t.passengers.length > 0 && t.passengers.every((p) => (t.done || []).includes(p)));
  const route = (a, b) => `${esc(title(a))} → ${esc(title(b))}`;
  const initials = (n) => String(n || "?").trim().split(/\s+/).slice(0, 2).map((x) => x[0]).join("").toUpperCase();
  function duration(dep, arr) {
    const p = (x) => { const [d, t] = String(x).split(" "); const [D, M, Y] = d.split("."); return new Date(`${Y}-${M}-${D}T${t}:00`); };
    const m = Math.round((p(arr) - p(dep)) / 60000);
    return m > 0 ? `${Math.floor(m / 60)} h ${m % 60} m` : "";
  }
  const seatText = (h, i) => { const b = { "В": "upper", "Н": "lower" }[(h.st || [])[i]]; return `${esc((h.s || [])[i] || "?")}${b ? ` ${b}` : ""}`; };
  const seatsLine = (h) => (h.s || []).map((_, i) => seatText(h, i)).join(", ") || "?";

  function tick() {
    document.querySelectorAll("[data-exp]").forEach((el) => {
      const left = secsLeft(el.dataset.exp);
      el.textContent = (el.dataset.pre || "") + (left > 0 ? clock(left) : "0:00");
      el.classList.toggle("low", left < 180);
    });
    document.querySelectorAll("[data-bar]").forEach((el) => {
      const left = Math.max(0, secsLeft(el.dataset.bar));
      el.firstElementChild.style.width = `${Math.min(100, left / 720 * 100).toFixed(2)}%`;
      el.classList.toggle("low", left < 180);
    });
    document.querySelectorAll("[data-at]").forEach((el) => { el.textContent = `checked ${ago(Math.max(0, S.now + elapsed() - +el.dataset.at))}`; });
    const c = $("#checkedAgo");
    if (c && S.status) c.textContent = `checked ${ago(Math.max(0, S.now + elapsed() - S.status.lastCheck))}`;
    if (currentView === "home" && !sheet && (activeHolds().length !== shownHolds || stateSig() !== shownSig)) renderTab();
  }

  // ------------------------------------------------------------------ Now
  function statusHtml() {
    const lost = pcLost();
    const line = (dot, b, rest, end = "") => `<div class="status" id="status"><div>${dot}<b>${b}</b>${rest}</div>${end}</div>`;
    if (S.live && !gotAt) return line(`<span class="dot"></span>`, "Connecting to your computer…", "");
    if (lost) return `<div class="problem bad" id="status">${ms("desktop_access_disabled")}<div><b>Your computer isn't responding</b><p>Last seen ${ago(elapsed())}. No new seats can be caught until it's back. Check that it's on and online.</p></div></div>`;
    if (!S.status) return line(`<span class="dot"></span>`, "Showing the bot's last message", "");
    if (S.status.state === "offline") return `<div class="problem warn" id="status">${ms("cloud_off")}<div><b>No answer from the railway site</b><p>The bot keeps trying. Seats it already holds stay held.</p></div></div>`;
    if (S.paused) return line(ms("pause", "fill"), "Paused", "", `<button class="resume" id="pauseToggle">Resume</button>`);
    if (S.status.state === "idle" || !S.trips.some((t) => t.active && !isCompleted(t))) return line(`<span class="dot"></span>`, "No active trips", "");
    if (S.status.state === "watching") return line(`<span class="dot on"></span>`, "Watching", `<span>·</span><span class="num" id="checkedAgo"></span>`,
      `<button class="icon-btn" id="pauseToggle" aria-label="Pause watching">${ms("pause_circle")}</button>`);
    return line(`<span class="dot"></span>`, esc(S.status.detail || S.status.state), "");
  }
  function holdBig(h) {
    const trip = S.trips.find((t) => t.id === h.trip), who = (h.n || []).join(", ");
    const pos = trip && (h.p || []).length === 1 ? trip.passengers.indexOf(h.p[0]) + 1 : 0;
    const dur = h.dep && h.arr ? duration(h.dep, h.arr) : "";
    return `<article class="block hold" data-o="${esc(h.o || "")}">
      <div class="bar" data-bar="${+h.exp}"><div></div></div>
      <div class="hold-in">
        <div class="hold-row"><div><div class="left-label">${ms("timer", "fill")}Left to pay</div><div class="cd big" role="timer" data-exp="${+h.exp}"></div></div>
          <div class="price"><b>${fmtSum(h.sum)}</b><span>so'm</span></div></div>
        <div class="journey"><div class="journey-date">${h.d ? dayLong(h.d) : ""}</div>
          <div class="journey-times"><div><b>${hhmm(h.dep)}</b><span>${esc(title(h.f))}</span></div>
            <div class="journey-mid"><i></i>${dur}<i></i></div>
            <div><b>${hhmm(h.arr)}${nextDay(h.dep, h.arr) ? "<sup>+1</sup>" : ""}</b><span>${esc(title(h.to))}</span></div></div></div>
        <div class="meta4"><div><small>Train</small><b>${esc(h.t)}</b></div><div><small>Car</small><b>${esc(h.c)}</b></div>
          <div><small>Seat</small><b>${(h.s || []).length === 1 ? `${esc(h.s[0])} ${BERTH((h.st || [])[0])}` : esc((h.s || []).join(", "))}</b></div>
          <div><small>Class</small><b>${h.cls ? esc(shortClass(h.cls)) : ""}</b></div></div>
        <div class="who"><span>${esc(initials((h.n || [])[0]))}</span><div><b>${esc(who)}</b>${pos ? `<small>Passenger ${pos} of ${trip.passengers.length}</small>` : ""}</div></div>
        ${h.pay ? `<button class="btn primary big pay-big" data-pay="${esc(h.pay)}">Pay ${fmtSum(h.sum)} so'm${ms("arrow_forward")}</button>` : `<p class="hint" style="padding:14px 0 0">The payment link is on its way.</p>`}
        ${S.v >= 3 && h.o ? `<div class="two"><button data-xfer>${ms("swap_horiz")}Transfer</button><button data-cancel>${ms("close")}Cancel</button></div>` : ""}
      </div></article>`;
  }
  const BERTH = (x) => { const b = { "В": "upper", "Н": "lower" }[x]; return b ? `<em>${b}</em>` : ""; };
  function holdSmall(h) {
    return `<article class="block tight hold" data-o="${esc(h.o || "")}">
      <div class="hold-small"><div class="route-line">${route(h.f, h.to)}${h.d ? ` · ${dayLong(h.d)}` : ""}${h.dep ? ` · <span class="num">${hhmm(h.dep)} → ${hhmm(h.arr)}</span>` : ""}</div>
        <div class="line"><div class="tleft"><div class="cd mid" role="timer" data-exp="${+h.exp}"></div><small>left to pay</small></div>
          <div class="pinfo"><b>${esc((h.n || []).join(", "))}</b><div>${esc(h.t)} · Car ${esc(h.c)} · Seat ${seatsLine(h)}${h.cls ? `<br>${esc(shortClass(h.cls))}` : ""}</div></div></div>
        <div class="btn-row">${h.pay ? `<button class="btn primary" data-pay="${esc(h.pay)}">Pay ${fmtSum(h.sum)} so'm</button>` : `<button class="btn primary" disabled>Link on its way</button>`}
          ${S.v >= 3 && h.o ? `<button class="btn" data-xfer>Transfer</button><button class="btn" data-cancel>Cancel</button>` : ""}</div></div>
      <div class="bar thin" data-bar="${+h.exp}"><div></div></div></article>`;
  }
  function cellHtml(c, v, t) {
    const wanted = !t.classes.length || t.classes.includes(c), over = t.maxPrice && v.price > t.maxPrice;
    const k = v.free <= 0 ? "sold" : !wanted ? "off" : over ? "over" : "match";
    const icon = k === "match" ? ms("check") : k === "over" ? ms("arrow_upward") : "";
    return `<span class="cell ${k}">${icon}<b>${esc(shortClass(c))}</b><span class="d">${v.free > 0 ? `${v.free} · ${fmtSum(v.price)}` : "sold out"}</span></span>`;
  }
  function boardHtml() {
    if (S.paused || !S.board) return "";
    const cards = [];
    for (const t of S.trips.filter((x) => x.active && !isCompleted(x))) {
      for (const d of [...t.dates].sort().filter((x) => x >= (S.today || iso(today())))) {
        const b = S.board.find((x) => x.from === t.from.code && x.to === t.to.code && x.date === d);
        const rows = !b ? `<p class="pad">Waiting for the first check…</p>` : !b.trains.length ? `<p class="pad">No trains on this date.</p>`
          : [...b.trains].sort((x, y) => hhmm(x.dep).localeCompare(hhmm(y.dep))).map((tr) => {
            const cells = Object.entries(tr.classes).sort(([x], [y]) => (t.classes.includes(y) ? 1 : 0) - (t.classes.includes(x) ? 1 : 0));
            const sold = !cells.length || cells.every(([, v]) => v.free <= 0);
            return `<div class="tr"><div class="tr-top"><b>${esc(tr.number)}</b><span class="t">${hhmm(tr.dep)} → ${hhmm(tr.arr)}${nextDay(tr.dep, tr.arr) ? "<sup>+1</sup>" : ""}</span>${sold ? `<span class="cell sold">sold out</span>` : ""}</div>
              ${sold ? "" : `<div class="cells">${cells.map(([c, v]) => cellHtml(c, v, t)).join("")}</div>`}</div>`;
          }).join("");
        cards.push(`<section class="board"><div class="board-head"><b>${esc(t.from.name.toUpperCase())} → ${esc(t.to.name.toUpperCase())} <span>· ${dayLong(d)}</span></b>${b ? `<small data-at="${+b.at}"></small>` : ""}</div>${rows}</section>`);
      }
    }
    if (!cards.length) return "";
    return `<div class="board-wrap"><h2>Right now on the railway site</h2>
      <div class="legend"><span class="cell match">${ms("check")}matches your trip</span><span class="cell over">${ms("arrow_upward")}above your max price</span><span class="cell sold">sold out</span><span class="cell off">not chosen</span></div>
      ${cards.join("")}<div class="note">Prices per seat in so'm.</div></div>`;
  }
  function renderNow() {
    const holds = activeHolds(), active = S.trips.filter((t) => !isCompleted(t));
    if (!active.length && !holds.length) {
      app.innerHTML = statusHtml() + `<div class="empty"><span class="empty-icon">${ms("radar")}</span><h2>Nothing to watch yet</h2>
        <p>Add a trip with your route and dates. The bot checks the railway site and holds a seat as soon as one appears, then you have about 12 minutes to pay.</p></div>`;
      setPrimary("New trip", () => render("trip", null));
      bindPause();
      return;
    }
    // The order on this page: seats to pay for, active trips, then the railway site.
    const n = holds.length, trips = [...active].sort((a, b) => Number(b.active) - Number(a.active));
    app.innerHTML = statusHtml() +
      (n ? `<div class="page-head" style="padding-top:8px"><h1 class="page-title">${n} seat${n === 1 ? "" : "s"} held</h1>
        <p class="lead">Pay before the timer ends or the seat goes back on sale.</p></div>` : `<h1 class="sr-only">Now</h1>`) +
      holds.map((h, i) => (i ? holdSmall(h) : holdBig(h))).join("") +
      (trips.length ? `<h2 class="h2-sec" ${n ? "" : `style="padding-top:8px"`}>Active trip${trips.length === 1 ? "" : "s"}</h2>${trips.map((t) => tripCard(t, holds)).join("")}` : "") +
      (S.v < 3 ? `<div class="problem warn" style="margin-top:16px">${ms("progress_activity")}<div><b>Restart the bot on your computer</b><p>It's on an older version, so live seats and passenger highlights can't show yet.</p></div></div>` : "") +
      boardHtml();
    app.querySelectorAll("[data-pay]").forEach((b) => {
      b.onclick = () => {
        if (!/^https:\/\//.test(b.dataset.pay)) return;
        if (inTelegram) tg.openLink(b.dataset.pay); else window.open(b.dataset.pay, "_blank", "noopener");
      };
    });
    app.querySelectorAll(".hold").forEach((card) => {
      const o = card.dataset.o, x = card.querySelector("[data-xfer]"), c = card.querySelector("[data-cancel]");
      if (x) x.onclick = () => openSheet(o);
      if (c) {
        c.onclick = () => confirmThen("Cancel this reservation? The seats go back on sale and the trip is paused.", async () => {
          if (await send({ a: "cancel", order: o })) { S.holds = S.holds.filter((h) => h.o !== o); toast("Cancelling…"); renderTab(); }
        });
      }
    });
    bindTripActions();
    bindPause();
    setPrimary("New trip", null);
  }
  function bindPause() {
    const b = $("#pauseToggle");
    if (b) b.onclick = () => { S.paused = !S.paused; send({ a: "pause", on: S.paused }); haptic(); renderTab(); };
  }

  // ------------------------------------------------------------------ transfer sheet
  function openSheet(order) {
    const h = S.holds.find((x) => x.o === order);
    sheet = { order, from: (h.p || [])[0], to: ((S.targets || {})[order] || [])[0]?.id || "" };
    drawSheet();
  }
  function closeSheet() { sheet = null; $("#sheet").hidden = true; renderTab(); }
  function drawSheet() {
    const h = S.holds.find((x) => x.o === sheet.order);
    if (!h) return closeSheet();
    const targets = (S.targets || {})[sheet.order] || [];
    const seatOf = (p) => seatText(h, (h.p || []).indexOf(p));
    const trip = S.trips.find((t) => t.id === h.trip);
    const busy = (trip ? trip.passengers : []).filter((p) => !targets.some((x) => x.id === p) && !(h.p || []).includes(p));
    const holderOf = (p) => (S.holds || []).find((x) => (x.p || []).includes(p));
    const to = targets.find((x) => x.id === sheet.to);
    const box = $("#sheet");
    box.hidden = false;
    box.innerHTML = `<div class="sheet xfer" role="dialog" aria-modal="true" aria-label="Transfer a seat"><div class="handle"></div>
      <div class="sheet-top"><div><h3>Transfer a seat</h3><p>The seat is released and booked again for the new passenger straight away.</p></div>
        <span class="time-pill">${ms("timer", "fill")}<span data-exp="${+h.exp}"></span></span></div>
      <div class="sheet-label">Seat of</div>
      <div class="pick-group">${(h.p || []).map((p, i) => `<button class="pick" data-from="${esc(p)}"><span class="radio ${p === sheet.from ? "on" : ""}"></span><span class="inf"><b>${esc((h.n || [])[i] || "Passenger")}</b><small>Car ${esc(h.c)} · Seat ${seatText(h, i)}</small></span></button>`).join("")}</div>
      <div class="down">${ms("arrow_downward")}</div>
      <div class="sheet-label close">Goes to</div>
      <div class="pick-group">${targets.map((x) => `<button class="pick" data-to="${esc(x.id)}"><span class="radio ${x.id === sheet.to ? "on" : ""}"></span><span class="inf"><b>${esc(x.name)}</b><small>${x.pos ? `Passenger ${x.pos} · still searching` : "Not on this trip"}</small></span></button>`).join("")}
        ${busy.map((p) => { const o = holderOf(p); return `<button class="pick" disabled><span class="radio none"></span><span class="inf"><b>${esc(paxName(p))}</b><small>${o ? `Already holds seat ${esc((o.s || [])[(o.p || []).indexOf(p)] || "?")}` : "Already has a ticket"}</small></span></button>`; }).join("")}
        ${!targets.length && !busy.length ? `<p class="pad">Nobody else can take this seat.</p>` : ""}</div>
      <button class="btn primary big" id="moveSeat" ${to ? "" : "disabled"}>Move the seat</button>
      ${to ? `<div class="caption">Seat ${seatOf(sheet.from)} goes to ${esc(to.name)}</div>` : ""}
      <button class="btn big" id="sheetBack" style="margin-top:8px;background:none">Back</button></div>`;
    box.onclick = (e) => { if (e.target === box) closeSheet(); };
    box.querySelectorAll("[data-from]").forEach((b) => { b.onclick = () => { sheet.from = b.dataset.from; haptic(); drawSheet(); }; });
    box.querySelectorAll("[data-to]").forEach((b) => { b.onclick = () => { sheet.to = b.dataset.to; haptic(); drawSheet(); }; });
    $("#sheetBack").onclick = closeSheet;
    $("#moveSeat").onclick = async () => {
      const body = { a: "transfer", order: sheet.order, from: sheet.from, to: sheet.to };
      if (await send(body)) { toast("Moving the seat…"); closeSheet(); }
    };
    tick();
  }

  // ------------------------------------------------------------------ Trips
  function personRow(t, p, i, holds) {
    const hold = holds.find((h) => h.trip === t.id && (h.p || []).includes(p));
    const paid = (t.done || []).includes(p);
    const k = paid ? "paid" : hold ? "held" : "";
    const st = paid ? `<span class="st">Paid</span>${ms("check", "bold")}`
      : hold ? `<span class="st" data-pre="Held · pay in " data-exp="${+hold.exp}"></span>${ms("check", "bold")}`
      : `<span class="st">Searching</span>${ms("progress_activity")}`;
    return `<div class="person ${k}"><span class="n">${i + 1}</span><span class="name">${esc(paxName(p))}</span>${st}</div>`;
  }
  function tripCard(t, holds) {
    const ds = (t.dates || []).filter((d) => d >= iso(today()));
    const shown = ds.length ? ds : t.dates;
    const cls = t.classes.length ? t.classes.map(shortClass).join(", ") : "Any class";
    const trains = t.trains && t.trains.length ? `${t.trains.join(", ")} only` : "Any train";
    return `<article class="trip ${t.active ? "" : "paused"}" data-id="${esc(t.id)}">
      <div class="pills">${t.active ? `<span class="pill go"><i></i>Watching</span>` : `<span class="pill off">${ms("pause", "fill")}Paused</span>`}
        ${t.mode === "buy" ? `<span class="pill">${ms("bolt", "fill")}Auto-buy</span>` : `<span class="pill line">${ms("notifications")}Notify only</span>`}</div>
      <h2>${route(t.from.name, t.to.name)}</h2>
      <div class="when">${shown.slice(0, 4).map(dayLong).join(" · ")}${shown.length > 4 ? ` +${shown.length - 4}` : ""}</div>
      <div class="kv"><span>Classes</span><span>${esc(cls)}</span>
        <span>Max price</span><span>${t.maxPrice ? `${fmtSum(t.maxPrice)} so'm` : "No limit"}</span>
        <span>Trains</span><span>${esc(trains)}</span></div>
      ${t.mode === "buy" ? `<div class="pax-label">PASSENGERS · IN PRIORITY ORDER</div><div class="rows">${t.passengers.map((p, i) => personRow(t, p, i, holds)).join("")}</div>` : ""}
      <div class="acts"><button data-act="edit">${ms("edit")}Edit</button><button data-act="ret">${ms("u_turn_left")}Return trip</button>
        ${t.active ? `<button data-act="toggle">${ms("pause")}Pause</button>` : `<button data-act="toggle" class="go">${ms("play_arrow", "fill")}Resume</button>`}
        <button data-act="del" class="red">${ms("delete")}Delete</button></div></article>`;
  }
  function ticketCard(t, holds) {
    const tickets = t.tk || [];
    return `<article class="ticket" data-id="${esc(t.id)}"><div class="ticket-top"><div class="when">${t.dates.slice(0, 3).map(dayLong).join(" · ")}</div><h2>${route(t.from.name, t.to.name)}</h2>
        ${t.mode === "buy" ? `<div class="rows">${t.passengers.map((p, i) => personRow(t, p, i, holds)).join("")}</div>` : ""}</div>
      ${tickets.map((x) => `<div class="perf"></div><div class="meta3"><div><small>Train</small><b>${esc(x.t)}</b></div><div><small>Car</small><b>${esc(x.c)}</b></div><div><small>Seat</small><b>${esc((x.s || []).join(", ") || "?")}</b></div></div>`).join("")}
      <div class="ticket-acts">${tickets.length ? `<button class="btn primary" data-act="get">${ms("download")}Get tickets</button>` : ""}
        <div class="two strong"><button data-act="ret">${ms("u_turn_left")}Return trip</button><button data-act="del" class="red">${ms("delete")}Delete</button></div></div></article>`;
  }
  function bindTripActions() {
    app.querySelectorAll(".trip, .ticket").forEach((el) => {
      const t = S.trips.find((x) => x.id === el.dataset.id), q = (a) => el.querySelector(`[data-act="${a}"]`) || {};
      q("get").onclick = async () => { if (guardPending() && await send({ a: "tickets", id: t.id })) toast("Sending your tickets to the chat"); };
      q("edit").onclick = () => guardPending() && render("trip", t.id);
      q("ret").onclick = () => guardPending() && render("trip", { ret: t });
      q("toggle").onclick = () => {
        t.active = !t.active;
        if (liveOn || !canBatch) send({ a: "toggle", id: t.id, active: t.active });
        else {
          const i = pending.findIndex((o) => o.a === "toggle" && o.id === t.id);
          if (i >= 0) pending.splice(i, 1); else pending.push({ a: "toggle", id: t.id, active: t.active });
        }
        haptic(); renderTab();
      };
      q("del").onclick = () => confirmThen("Delete this trip?", () => {
        S.trips = S.trips.filter((x) => x !== t);
        if (liveOn || !canBatch) send({ a: "del", id: t.id });
        else {
          for (let i = pending.length - 1; i >= 0; i--) if (pending[i].id === t.id) pending.splice(i, 1);
          pending.push({ a: "del", id: t.id });
        }
        haptic(); renderTab();
      });
    });
  }
  function renderTrips() {
    const holds = activeHolds(), completed = S.trips.filter(isCompleted), active = S.trips.filter((t) => !isCompleted(t));
    const list = tripsTab === "completed" ? completed : active;
    active.sort((a, b) => Number(b.active) - Number(a.active));
    const body = list.length ? list.map((t) => (tripsTab === "completed" ? ticketCard(t, holds) : tripCard(t, holds))).join("")
      : tripsTab === "completed" ? `<div class="empty small"><h2 style="margin-top:0">No completed trips yet</h2><p>Paid tickets show up here with their train, car and seat.</p></div>`
      : `<div class="empty small"><span class="empty-icon quiet">${ms("route")}</span><h2>No active trips</h2><p>Add a route and dates. The bot starts watching as soon as you save.</p></div>`;
    app.innerHTML = `<div class="page-head"><h1 class="page-title">Trips</h1>
        <div class="seg" id="tripTabs" role="group" aria-label="Show trips" style="margin-top:14px">${[["active", "Active", active], ["completed", "Completed", completed]]
          .map(([v, n, l]) => `<button data-v="${v}" class="${tripsTab === v ? "on" : ""}" aria-pressed="${tripsTab === v}">${n} <span class="dim">${l.length}</span></button>`).join("")}</div></div>
      ${pending.length ? `<button id="undoAll" type="button">Undo changes</button>` : ""}${body}`;
    $("#tripTabs").querySelectorAll("button").forEach((b) => { b.onclick = () => { tripsTab = b.dataset.v; haptic(); renderTab(); }; });
    if ($("#undoAll")) $("#undoAll").onclick = () => { S.trips = JSON.parse(originalTrips); pending.length = 0; haptic(); renderTab(); };
    bindTripActions();
    if (pending.length) setPrimary(`Save ${pending.length} change${pending.length === 1 ? "" : "s"}`, () => send({ a: "batch", ops: pending }));
    else setPrimary("New trip", () => render("trip", null));
    closingConfirm(pending.length > 0);
  }

  // ------------------------------------------------------------------ Activity
  // shortcut: a copy of the wording in uzrailbot/ui/ui.js, share one file if they start to differ
  const EV = {
    session_start: null, heartbeat: null, notify: null, plan: null, miniapp: null, http: null, ws: null, check: null,
    start: (o) => [o.updated_from ? "ok" : "", o.updated_from ? `Updated v${o.updated_from} → v${o.version} and restarted` : `Bot v${o.version} started on this PC`],
    stop: () => ["", "Bot stopped"],
    state: (o) => (o.state === "watching" ? null : o.state === "offline" ? ["bad", "Lost the connection to the railway site"]
      : ["", `Status: ${o.state}${o.detail ? ` (${o.detail})` : ""}`]),
    trip_trains: (o) => ["", o.trains.length ? `Trip now watches only train ${o.trains.join(", ")}` : "Trip now watches all trains"],
    trip_deleted: () => ["", "Trip deleted"],
    availability: (o) => o.free > 0
      ? ["ok", `${o.free} seat${o.free === 1 ? "" : "s"} · ${shortClass(o.cls)} · train ${o.train} · ${fmtDay(o.date)} · ${fmtSum(o.price)} so'm`]
      : ["", `Seats sold out again on train ${o.train} (${shortClass(o.cls)})`],
    detect: (o) => ["ok", `New seats on ${fmtDay(o.date)}, booking now`],
    fanout: (o) => ["", `Trying ${o.cars.length} cars at once on train ${o.train}`],
    reserve_sent: (o) => ["", `Booking request sent for car ${o.car} (${o.ms} ms)`],
    reserve_refused: (o) => ["warn", `Railway refused car ${o.car}: ${o.message || o.status}`],
    settled: (o) => [o.result === "RESERVED" ? "ok" : "warn", `Car ${o.car || "?"}: ${o.result === "RESERVED" ? "reserved" : o.result.toLowerCase()}${o.ms ? ` after ${o.ms} ms` : ""}`],
    held: (o) => ["ok", `Held seats ${(o.seats || []).join(", ")} on train ${o.train}, car ${o.car} in ${o.took_s} s`],
    extra_hold_cancelled: (o) => ["", `Released a duplicate hold in car ${o.car}`],
    seats_gone: () => ["warn", "The seats were gone before we could book"],
    missed: (o) => ["bad", `Missed: ${o.why}`],
    pay_link: () => ["", "Payment link ready"],
    pay_link_fail: () => ["warn", "Could not make the payment link yet, retrying"],
    transfer_start: (o) => ["", `Moving the seat in car ${o.car} of train ${o.train} to another passenger`],
    transfer_attempt: (o) => [o.result === "RESERVED" ? "ok" : "warn", o.result === "RESERVED" ? "Seat moved to the new passenger" : `Re-booking the seat, try ${o.attempt}`],
    transfer_error: (o) => ["bad", `Transfer failed: ${o.error}`],
    user_transfer: (o) => [o.ok ? "ok" : "warn", o.ok ? "You moved a held seat to another passenger" : "A seat transfer didn't work"],
    passengers_refreshed: (o) => ["", `Saved passengers reloaded (${o.count})`],
    renew_attempt: null, renew_error: null,
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
    trip_saved: (o) => ["", `Trip saved: ${o.dates.length} date${o.dates.length === 1 ? "" : "s"}, ${o.classes.map(shortClass).join(", ")}`],
    account_saved: (o) => ["ok", `Railway account connected (${o.passengers} saved passenger${o.passengers === 1 ? "" : "s"})`],
    learned_cars: (o) => ["", `Learned the car numbers of train ${o.train}`],
    check_fail: (o) => ["warn", `Check failed: ${o.error}`],
    http_fail: (o) => ["warn", `Network problem${o.error ? `: ${o.error}` : ""}`],
    book_error: (o) => ["bad", `Booking error: ${o.error}`],
    cars_retry: () => ["", "Car list was empty, asking again"],
    rate_limited: (o) => ["warn", `The railway site asked the bot to slow down: ${String(o.path).includes("reserve") ? "booking" : "that request"} waits ${o.pause_s} s`],
    booking_paused: () => ["warn", "Booking waits for the railway's limit to pass; still watching"],
    seats_unusable: (o) => ["", `The seats listed on train ${o.train} just failed to book; waiting until the list changes`],
    user_report: () => ["ok", "You reported a problem"],
    share_logs: (o) => ["", o.on ? "Usage logs: sharing turned on" : "Usage logs: sharing turned off"],
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
  const BOOKING = new Set(["availability", "detect", "fanout", "reserve_sent", "reserve_refused", "settled", "held",
    "extra_hold_cancelled", "seats_gone", "missed", "pay_link", "transfer_start", "transfer_attempt", "user_transfer", "hold_end", "paid", "user_cancel_hold",
    "recover_hold"]);
  function markOf(r, tone) {
    if (r.kind === "held") return ["held", "event_seat"];
    if (r.kind === "paid") return ["paid", "check"];
    if (tone === "bad") return ["bad", "desktop_access_disabled"];
    if (tone === "warn") return ["warn", "speed"];
    if (tone === "ok") return ["ok", "check_circle"];
    return ["info", "radar"];
  }
  function renderActivity() {
    const all = (S.activity || []).map((r) => [r, describe(r)]).filter(([, d]) => d).reverse();
    const isBook = ([r]) => BOOKING.has(r.kind), isBad = ([, d]) => d[0] === "bad" || d[0] === "warn";
    const rows = all.filter((x) => feedFilter === "all" || (feedFilter === "book" ? isBook(x) : isBad(x)));
    const dayName = (ts) => { const d = new Date(ts * 1000); return d.toDateString() === new Date().toDateString() ? "TODAY" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }).toUpperCase(); };
    let last = "", html = "";
    for (const [r, d] of rows) {
      const name = r.ts ? dayName(r.ts) : "";
      if (name !== last) { html += `<div class="day">${name}</div>`; last = name; }
      const [k, icon] = markOf(r, d[0]);
      html += `<div class="ev"><time>${esc(String(r.t || "").slice(0, 5))}</time><span class="mark ${k}">${ms(icon)}</span><span>${esc(d[1])}</span></div>`;
    }
    app.innerHTML = `<div class="page-head"><h1 class="page-title">Activity</h1>
      <div class="filters" id="feedTabs" role="group" aria-label="Filter">${[["all", "All", all.length], ["book", "Bookings", all.filter(isBook).length], ["bad", "Problems", all.filter(isBad).length]]
        .map(([v, n, c]) => `<button data-v="${v}" class="${feedFilter === v ? "on" : ""}" aria-pressed="${feedFilter === v}">${n}<span>${c}</span></button>`).join("")}</div></div>
      <div class="feed">${html || `<p class="pad" style="padding-left:0">${S.live ? "Nothing yet." : "The activity feed needs your computer connected."}</p>`}</div>`;
    $("#feedTabs").querySelectorAll("button").forEach((b) => { b.onclick = () => { feedFilter = b.dataset.v; haptic(); renderTab(); }; });
    setPrimary("New trip", null);
  }

  // ------------------------------------------------------------------ Settings
  function renderSettings() {
    const connected = S.email && S.hasPw;
    const sw = (id, name, sub, on, off) => `<label class="row"><span class="inf"><b>${name}</b><small>${sub}</small></span>
      <span class="switch"><input type="checkbox" id="${id}" aria-label="${name}" ${on ? "checked" : ""} ${off ? "disabled" : ""}><span></span></span></label>`;
    app.innerHTML = `<div class="page-head" style="padding-bottom:4px"><h1 class="page-title">Settings</h1></div>
      <div class="group-label" style="margin-top:18px">RAILWAY ACCOUNT</div>
      <div class="set-block acct"><span class="av">${ms("person", "fill")}</span><div class="inf"><b>${connected ? esc(S.email) : "Not connected yet"}</b><small>${connected ? "Signed in" : "Add your railway login"}</small></div>
        <button class="link" id="accBtn">${connected ? "Change" : "Connect"}</button></div>
      <div class="group-label">BOT</div>
      <div class="set-block">
        ${sw("pauseAll", "Watching", "Check the railway site for all active trips", !S.paused, S.v < 3)}
        ${sw("autoUp", "Update automatically", "Install new versions of the bot on your computer", S.autoUpdate !== false, !liveOn)}
        ${sw("shareLogs", "Share usage logs", "Sends activity logs so we can fix problems faster. No names, passport numbers or passwords.", S.shareLogs !== false, !liveOn)}
        <div class="row" style="min-height:52px"><span class="inf"><b>Version</b></span><span class="dim num">${esc(S.version || "–")}</span></div>
      </div>
      <div class="group-label">REPORT A PROBLEM</div>
      <div class="set-block report">
        <label for="reportText" class="sr-only">What happened?</label>
        <textarea id="reportText" rows="4" maxlength="1000" placeholder="What happened? Add the train and time if you can."></textarea>
        <div class="report-foot"><span>This session's log is attached.</span><button class="btn primary" id="reportSend" ${S.v < 3 ? "disabled" : ""}>${ms("send")}Send</button></div>
      </div>`;
    $("#accBtn").onclick = () => guardPending() && render("account");
    $("#pauseAll").onchange = (e) => { S.paused = !e.target.checked; send({ a: "pause", on: S.paused }); };
    $("#autoUp").onchange = (e) => { S.autoUpdate = e.target.checked; send({ a: "autoupdate", on: S.autoUpdate }); };
    $("#shareLogs").onchange = (e) => { S.shareLogs = e.target.checked; send({ a: "sharelogs", on: S.shareLogs }); };
    $("#reportSend").onclick = async () => {
      const note = $("#reportText").value.trim();
      if (!note) return toast("Write what happened first");
      if (await send({ a: "report", note })) { $("#reportText").value = ""; toast("Report sent. Thank you!"); }
    };
    setPrimary("New trip", null);
  }

  function renderTab() {
    shownSig = stateSig();
    shownHolds = activeHolds().length;
    setBack(null);
    document.querySelectorAll("#tabs button").forEach((b) => {
      const on = b.dataset.tab === tab;
      b.classList.toggle("on", on);
      if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    $("#nowBadge").hidden = !shownHolds;
    $("#nowBadge").textContent = shownHolds;
    ({ now: renderNow, trips: renderTrips, activity: renderActivity, settings: renderSettings })[tab]();
    if (S.demo) app.insertAdjacentHTML("afterbegin", `<div class="banner">This is a preview. Open it from your bot's Trips &amp; settings button.</div>`);
    tick();
  }
  document.querySelectorAll("#tabs button").forEach((b) => { b.onclick = () => { tab = b.dataset.tab; window.scrollTo(0, 0); haptic(); renderTab(); }; });

  // ------------------------------------------------------------------ views
  function render(view, arg) {
    window.scrollTo(0, 0);
    app.onclick = app.oninput = app.onchange = null;      // the editor's unsaved-changes watcher
    closingConfirm(false);
    swipes(view === "home");
    currentView = view;
    const home = view === "home";
    $("#tabs").hidden = !home;
    document.body.classList.toggle("has-tabs", home);
    if (home) return renderTab();
    if (view === "trip") return renderTrip(arg);
    if (view === "account") return renderAccount();
  }

  // ------------------------------------------------------------------ trip editor
  function renderTrip(id) {
    const ret = id && id.ret;           // "Return trip" on a card: the same trip, the other way, dates still to pick
    const src = id && !ret ? S.trips.find((t) => t.id === id) : null;
    const d = ret ? { ...JSON.parse(JSON.stringify(ret)), id: undefined, from: ret.to, to: ret.from, dates: [], trains: [], active: true }
      : src ? JSON.parse(JSON.stringify(src)) : {
        from: null, to: null, dates: [], classes: [], maxPrice: 0, mode: "buy",
        passengers: [], speed: 1, trains: [], together: false, active: true,
      };
    d.trains = d.trains || [];
    d.dates = (d.dates || []).filter((x) => x >= iso(today()));
    let month = d.dates.length ? new Date(d.dates[0] + "T00:00:00") : today();
    month = new Date(month.getFullYear(), month.getMonth(), 1);
    setBack(() => render("home"));
    const sec = (n, name, sumId) => `<div class="sec"><i>${n}</i><b>${name}</b><span id="${sumId}"></span></div>`;
    app.innerHTML = `<div class="page-head"><h1 class="page-title">${src ? "Edit trip" : ret ? "Return trip" : "New trip"}</h1></div>
      <div class="sec" style="padding-top:24px"><i>1</i><b>Route</b></div>
      <div class="card2"><div class="stations"><div id="fromBox"></div><div class="st-line"></div><div id="toBox"></div>
        <button class="swap" id="swap" aria-label="Swap stations">${ms("swap_vert")}</button></div></div>
      ${sec(2, "Dates", "dateSum")}
      <div class="card2" style="padding:12px 10px 10px">
        <div class="date-tools"><button id="next7" type="button">Next 7 days</button><button id="rangeBtn" type="button" aria-pressed="false">${ms("date_range")}<span id="rangeText">Pick a range</span></button></div>
        <div class="cal-head"><b id="monthName"></b><div><button class="icon-btn" id="prevM" aria-label="Previous month">${ms("chevron_left")}</button><button class="icon-btn" id="nextM" aria-label="Next month">${ms("chevron_right")}</button></div></div>
        <div class="dow">${["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"].map((x) => `<span>${x}</span>`).join("")}</div>
        <div class="cal" id="cal"></div>
      </div>
      ${sec(3, "Trains", "trainSum")}
      <div class="card2" id="trainList"></div>
      ${sec(4, "Classes", "classSum")}
      <div class="tiles" id="classes"></div>
      <details class="other" id="otherClasses" hidden><summary id="otherCount"></summary><div class="tiles" id="classesOther"></div></details>
      <div class="hint" id="classHint"></div>
      <div class="sec"><i>5</i><b>Maximum price</b><span>per ticket</span></div>
      <div class="card2 price-box"><div class="price-in"><input id="maxPrice" inputmode="numeric" placeholder="No limit" aria-label="Maximum price per ticket"><span>so'm</span></div>
        <p>Trains above this price are skipped. Leave it empty for no limit.</p></div>
      <div class="sec"><i>6</i><b>When a seat appears</b></div>
      <div class="card2" id="mode">
        <button class="opt" data-v="buy"><span class="radio"></span><span><b>Auto-buy</b><small>The bot holds the seat right away. You get about 12 minutes to pay.</small></span></button>
        <button class="opt" data-v="notify"><span class="radio"></span><span><b>Notify only</b><small>You get a message and book it yourself.</small></span></button>
      </div>
      <div id="paxSection">
        ${sec(7, "Passengers", "paxSum")}
        <div class="card2 clip"><div id="pax"></div>
          <button class="add-p" id="addNew" type="button">${ms("person_add")}Add new passenger</button>
          <button class="add-p" id="paxRefresh" type="button" style="color:var(--text-2)">${ms("history")}Refresh list</button></div>
        <label class="card2 row" style="margin-top:8px"><span class="inf"><b>Keep everyone together</b><small>Only hold seats when all passengers fit in the same car</small></span>
          <span class="switch"><input type="checkbox" id="together" aria-label="Keep everyone together"><span></span></span></label>
      </div>
      <details class="adv" id="more"><summary>${ms("expand_more", "closed-i")}${ms("expand_less", "open-i")}<b>Advanced</b></summary>
        <div class="card2 adv-in"><b style="font-weight:600">Check speed</b><div class="seg" id="speed"></div>
          <p>Faster checks catch more seats, but the railway site may ask the bot to slow down.</p></div></details>
      <div style="height:24px"></div>`;

    // stations
    function stationBox(key, label) {
      const box = $(key === "from" ? "#fromBox" : "#toBox");
      if (d[key]) {
        box.innerHTML = `<div class="station">${ms("search")}<div><small>${label}</small><button class="st-name picked" aria-label="Change ${label}">${esc(title(d[key].name))}</button></div></div>`;
        box.querySelector("button").onclick = () => { d[key] = null; stationBox(key, label); box.querySelector("input").focus(); validate(); refreshRoute(); };
        return;
      }
      box.innerHTML = `<div class="station">${ms("search")}<div><label for="st-${key}"><small>${label}</small></label>
        <input id="st-${key}" placeholder="${key === "from" ? "Tashkent" : "Samarkand"}" autocomplete="off"></div></div><div class="suggest" hidden></div>`;
      const input = box.querySelector("input"), sug = box.querySelector(".suggest");
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); const first = sug.querySelector("button"); if (first) first.click(); }
      });
      input.addEventListener("input", () => {
        const found = searchStations(input.value);
        if (!found.length) {
          const typed = norm(input.value).length >= 2;
          sug.hidden = !typed;
          sug.innerHTML = typed ? `<div class="no-hit">${STATIONS.length ? "No station found. Try another spelling (English, Uzbek or Russian)." : "Loading stations…"}</div>` : "";
          return;
        }
        sug.hidden = false;
        sug.innerHTML = found.map((s) => `<button data-c="${esc(s.c)}"><span>${esc(title(s.en))}</span><small>${esc(s.uz && norm(s.uz) !== norm(s.en) ? title(s.uz) : (s.uzb ? "" : "abroad"))}</small></button>`).join("");
        sug.querySelectorAll("button").forEach((b) => {
          b.onclick = () => {
            const s = STATIONS.find((x) => x.c === b.dataset.c);
            const other = key === "from" ? d.to : d.from;
            if (other && other.code === s.c) { toast("Pick a different station"); return; }
            d[key] = { code: s.c, name: s.en };
            haptic(); stationBox(key, label); validate(); refreshRoute();
          };
        });
      });
    }
    stationBox("from", "From");
    stationBox("to", "To");
    $("#swap").onclick = () => { [d.from, d.to] = [d.to, d.from]; stationBox("from", "From"); stationBox("to", "To"); haptic(); validate(); refreshRoute(); };

    // calendar
    let picking = false, rangeFrom = "";
    function addDates(list) {
      const all = [...new Set([...d.dates, ...list])].sort();
      if (all.length > MAX_DATES) toast(`Up to ${MAX_DATES} dates per trip`);
      d.dates = all.slice(0, MAX_DATES);
    }
    function daysBetween(a, b) {
      const out = [], end = new Date((a < b ? b : a) + "T00:00:00");
      for (let x = new Date((a < b ? a : b) + "T00:00:00"); x <= end; x.setDate(x.getDate() + 1)) out.push(iso(x));
      return out;
    }
    function drawCal() {
      $("#monthName").textContent = month.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
      $("#rangeText").textContent = !picking ? "Pick a range" : rangeFrom ? "Now the last day" : "Tap the first day";
      $("#rangeBtn").setAttribute("aria-pressed", picking);
      const first = (month.getDay() + 6) % 7;
      const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
      const t0 = today(), last = new Date(t0); last.setDate(last.getDate() + 90);
      let html = "";
      for (let i = 0; i < first; i++) html += "<span></span>";
      for (let day = 1; day <= days; day++) {
        const dt = new Date(month.getFullYear(), month.getMonth(), day), k = iso(dt);
        const prev = new Date(dt), next = new Date(dt);
        prev.setDate(day - 1); next.setDate(day + 1);
        const on = d.dates.includes(k), dow = (dt.getDay() + 6) % 7;
        const off = dt < t0 || dt > last;
        const cls = ["day-b", on ? "on" : "", k === iso(t0) && !on ? "today" : "", on && dow > 0 && d.dates.includes(iso(prev)) ? "join-l" : "", on && dow < 6 && d.dates.includes(iso(next)) ? "join-r" : ""].join(" ");
        const name = dt.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
        html += `<button class="${cls}" data-d="${k}" aria-label="${name}" aria-pressed="${on}" ${off ? "disabled" : ""}>${day}</button>`;
      }
      $("#cal").innerHTML = html;
      $("#cal").querySelectorAll(".day-b:not(:disabled)").forEach((b) => {
        b.onclick = () => {
          const k = b.dataset.d;
          if (picking && !rangeFrom) { rangeFrom = k; addDates([k]); }
          else if (picking) { addDates(daysBetween(rangeFrom, k)); picking = false; rangeFrom = ""; }
          else d.dates = d.dates.includes(k) ? d.dates.filter((x) => x !== k) : [...d.dates, k].sort();
          haptic(); drawCal(); validate(); refreshRoute(); refocus(`.day-b[data-d="${k}"]`);
        };
      });
      $("#dateSum").textContent = d.dates.length ? d.dates.slice(0, 3).map((x) => fmtDay(x)).join(", ") + (d.dates.length > 3 ? ` +${d.dates.length - 3}` : "") : "";
      const prev = new Date(month.getFullYear(), month.getMonth(), 0);
      $("#prevM").disabled = prev < new Date(t0.getFullYear(), t0.getMonth(), 1);
    }
    $("#rangeBtn").onclick = () => { picking = !picking; rangeFrom = ""; haptic(); drawCal(); };
    $("#next7").onclick = () => {
      const t0 = today(), end = new Date(t0); end.setDate(end.getDate() + 6);
      addDates(daysBetween(iso(t0), iso(end)));
      haptic(); drawCal(); validate(); refreshRoute();
    };
    $("#prevM").onclick = () => { month = new Date(month.getFullYear(), month.getMonth() - 1, 1); drawCal(); };
    $("#nextM").onclick = () => { month = new Date(month.getFullYear(), month.getMonth() + 1, 1); drawCal(); };
    drawCal();

    // classes: the ones this route's trains carry come first, with how many trains have them
    function relevantTrains() {
      let list = routeTrains(d.from && d.from.code, d.to && d.to.code);
      if (d.trains.length) list = list.filter((t) => d.trains.includes(t.n));
      else if (d.dates.length) {
        const running = list.filter((t) => d.dates.some((x) => runsOn(t, x) !== false));
        if (running.length) list = running;
      }
      return list;
    }
    function drawClasses() {
      const trains = relevantTrains();
      const count = {};
      for (const t of trains) for (const c of t.c) count[c] = (count[c] || 0) + 1;
      const known = trains.length && Object.keys(count).length;
      const extra = Object.keys(count).filter((c) => !CLASSES.some((k) => k.c === c)).map(classMeta);
      const main = known ? [...CLASSES.filter((k) => count[k.c]), ...extra] : CLASSES;
      const rest = known ? CLASSES.filter((k) => !count[k.c]) : [];
      const tile = (k) => { const on = d.classes.includes(k.c); return `<button class="tile ${on ? "on" : ""}" data-c="${k.c}" aria-pressed="${on}"><div><b>${esc(k.name)}</b><small>${count[k.c] ? `${count[k.c]} train${count[k.c] === 1 ? "" : "s"}` : esc(k.sub)}</small></div>${on ? ms("check") : ""}</button>`; };
      $("#classes").innerHTML = main.map(tile).join("");
      $("#classesOther").innerHTML = rest.map(tile).join("");
      $("#otherClasses").hidden = !rest.length;
      $("#otherCount").textContent = `Other classes (${rest.length}, not seen on this route)`;
      if (rest.some((k) => d.classes.includes(k.c))) $("#otherClasses").open = true;
      document.querySelectorAll("#classes .tile, #classesOther .tile").forEach((b) => {
        b.onclick = () => {
          const c = b.dataset.c;
          d.classes = d.classes.includes(c) ? d.classes.filter((x) => x !== c) : [...d.classes, c];
          haptic(); drawClasses(); refocus(`.tile[data-c="${c}"]`);
        };
      });
      $("#classSum").textContent = d.classes.length ? `${d.classes.length} chosen` : "Any class";
      $("#classHint").textContent = !d.from || !d.to ? "Pick the route to see which classes its trains have." : !TT ? "Loading the timetable…" : "";
    }
    const mp = $("#maxPrice");
    mp.value = d.maxPrice ? fmtSum(d.maxPrice) : "";
    mp.oninput = () => { const v = mp.value.replace(/\D/g, ""); d.maxPrice = v ? parseInt(v, 10) : 0; };
    mp.onblur = () => { mp.value = d.maxPrice ? fmtSum(d.maxPrice) : ""; };

    // mode
    function drawMode() {
      $("#mode").querySelectorAll("button").forEach((b) => {
        const on = b.dataset.v === d.mode;
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", on);
        b.querySelector(".radio").classList.toggle("on", on);
      });
      $("#paxSection").hidden = d.mode !== "buy";
      validate();
    }
    $("#mode").querySelectorAll("button").forEach((b) => { b.onclick = () => { d.mode = b.dataset.v; haptic(); drawMode(); }; });

    // passengers: tap in the order the bot should book them, tap again to remove
    function drawPax() {
      const chosen = d.passengers.filter((p) => S.pax.some((x) => x.i === p));
      d.passengers = chosen;
      $("#paxSum").textContent = `${chosen.length} of ${MAX_PAX}`;
      if (!S.pax.length) {
        $("#pax").innerHTML = `<p class="pad">${S.email && S.hasPw ? "Your saved passengers aren't on this page yet. Tap Refresh list."
          : "Connect your railway account first in Settings. Your saved passengers then appear here."}</p>`;
        return;
      }
      const full = chosen.length >= MAX_PAX;
      $("#pax").innerHTML = `<div class="slots-box"><div class="slots">${[0, 1, 2, 3].map((i) => {
        const id = chosen[i];
        return `<div class="slot ${id ? "on" : ""}"><small>${i + 1}</small><b>${id ? esc(paxName(id).split(" ")[0]) : "Empty"}</b></div>`;
      }).join("")}</div><p>Tap people in the order the bot should book them. Tap again to remove.</p></div>` +
        S.pax.map((p) => {
          const idx = chosen.indexOf(p.i), on = idx >= 0;
          return `<button class="pax-pick ${on ? "on" : ""}" data-i="${esc(p.i)}" aria-pressed="${on}" ${full && !on ? "disabled" : ""}${on ? ` aria-label="${esc(p.n)}, priority ${idx + 1}"` : ""}>
            <span class="av">${esc(initials(p.n))}</span><span class="name">${esc(p.n)}</span><span class="mk ${on ? "on" : ""}">${on ? idx + 1 : ""}</span></button>`;
        }).join("");
      $("#pax").querySelectorAll(".pax-pick").forEach((b) => {
        b.onclick = () => {
          const id = b.dataset.i;
          d.passengers = d.passengers.includes(id) ? d.passengers.filter((x) => x !== id) : [...d.passengers, id];
          haptic(); drawPax(); validate(); refocus(`.pax-pick[data-i="${CSS.escape(id)}"]`);
        };
      });
    }
    drawPax();
    $("#addNew").onclick = () => {
      const site = "https://eticket.uzrailpass.uz";       // saved passengers live on the railway site, not in the bot
      toast("Add them under My fellow travelers, then tap Refresh list");
      if (inTelegram) tg.openLink(site); else window.open(site, "_blank", "noopener");
    };
    $("#paxRefresh").onclick = () => confirmThen("Reload your saved passengers? Unsaved changes on this page are lost, and the bot replies with the fresh list.", () => send({ a: "pax" }));
    $("#together").checked = !!d.together;
    $("#together").onchange = (e) => { d.together = e.target.checked; };

    // advanced
    $("#speed").innerHTML = SPEEDS.map((s) => `<button data-v="${s.v}">${s.label} · ${s.sub.replace("every ", "")}</button>`).join("");
    function drawSpeed() { $("#speed").querySelectorAll("button").forEach((b) => { b.classList.toggle("on", +b.dataset.v === d.speed); b.setAttribute("aria-pressed", +b.dataset.v === d.speed); }); }
    $("#speed").querySelectorAll("button").forEach((b) => { b.onclick = () => { d.speed = +b.dataset.v; haptic(); drawSpeed(); }; });
    drawSpeed();

    // trains: every train by default; tap some to watch only those
    let routeKey = d.from && d.to ? d.from.code + ">" + d.to.code : "";
    let showAll = false, any = !d.trains.length;
    function drawTrains() {
      const key = d.from && d.to ? d.from.code + ">" + d.to.code : "";
      if (key !== routeKey) { d.trains = []; routeKey = key; showAll = false; any = true; }   // a new route: back to all trains
      const list = $("#trainList");
      if (!key) { list.innerHTML = `<p class="pad">Pick From and To to see the trains on this route.</p>`; $("#trainSum").textContent = ""; return; }
      if (!TT) { list.innerHTML = `<p class="pad">Loading the timetable…</p>`; return; }
      const rows = routeTrains(d.from.code, d.to.code);
      const sameRoute = src && src.from.code === d.from.code && src.to.code === d.to.code;
      for (const [n, dep, arr] of (sameRoute && src.kt) || []) {         // trains the PC saw that the timetable lacks
        if (!rows.some((r) => r.n === n)) rows.push({ n, b: "", c: [], dep, arr, nights: 0, fromDay: 0, r: null, w: null });
      }
      for (const n of d.trains) if (!rows.some((r) => r.n === n)) rows.push({ n, b: "", c: [], dep: "", arr: "", nights: 0, r: null, w: null });
      const off = (t) => d.dates.length && d.dates.every((x) => runsOn(t, x) === false) ? 1 : 0;
      rows.sort((x, y) => off(x) - off(y) || (x.dep || "99").localeCompare(y.dep || "99"));
      const LIMIT = 6;
      $("#trainSum").textContent = any ? "Any train" : `${d.trains.length} of ${rows.length}`;
      if (!rows.length) { list.innerHTML = `<p class="pad">No trains for this route in the timetable. Every train is watched, and the bot shows the live list after you save.</p>`; return; }
      const shown = showAll ? rows : rows.filter((t, i) => i < LIMIT || d.trains.includes(t.n));
      list.innerHTML = `<label class="row" style="min-height:52px"><span class="inf"><b>Any train</b>${any ? "<small>Watching every train that runs on your dates</small>" : ""}</span><span class="switch"><input type="checkbox" id="anyTrain" aria-label="Any train" ${any ? "checked" : ""}><span></span></span></label>` +
        shown.map((t) => {
          const runs = d.dates.map((x) => runsOn(t, x));
          const no = d.dates.length && runs.every((x) => x === false) && !d.trains.includes(t.n);
          const some = d.dates.length && !no && runs.some((x) => x === false);
          const days = runText(t.r, t.w) || "Daily";
          const note = no ? "Doesn't run on your dates" : some ? `Runs on ${runs.filter((x) => x !== false).length} of ${d.dates.length} dates` : "";
          const on = !any && d.trains.includes(t.n);
          return `<button class="tr-row ${any ? "off" : ""}" data-t="${esc(t.n)}" aria-pressed="${on}" ${no || any ? "disabled" : ""}><span class="box ${on ? "on" : ""}">${on ? ms("check") : ""}</span>
            <span class="inf"><span class="t1"><b>${esc(t.n)}</b><span>${esc(t.dep || "")}${t.dep ? " → " : ""}${esc(t.arr || "")}${t.nights ? `<sup>+${t.nights}</sup>` : ""}</span></span>
            <div class="t2">${esc(days)}${t.c.length ? ` · ${esc(t.c.map(shortClass).join(", "))}` : ""}</div>${note ? `<div class="t3">${note}</div>` : ""}</span></button>`;
        }).join("") +
        (rows.length > LIMIT ? `<button class="more-t" id="moreTrains" type="button">${showAll ? "Show fewer" : `Show all ${rows.length} trains`}</button>` : "");
      $("#anyTrain").onchange = (e) => {
        any = e.target.checked;
        if (any) d.trains = [];
        haptic(); drawTrains(); drawClasses();
      };
      if ($("#moreTrains")) $("#moreTrains").onclick = () => { showAll = !showAll; drawTrains(); };
      list.querySelectorAll("[data-t]").forEach((b) => {
        b.onclick = () => {
          const n = b.dataset.t;
          d.trains = d.trains.includes(n) ? d.trains.filter((x) => x !== n) : [...d.trains, n];
          haptic(); drawTrains(); drawClasses(); refocus(`.tr-row[data-t="${CSS.escape(n)}"]`);
        };
      });
    }
    function refreshRoute() { drawTrains(); drawClasses(); }
    onTimetable = () => { if (document.getElementById("trainList")) refreshRoute(); };
    drawTrains();
    drawClasses();
    drawMode();

    function problems() {
      if (!d.from || !d.to) return "Choose From and To";
      if (!d.dates.length) return "Pick at least one date";
      if (d.mode === "buy" && !d.passengers.length) return "Add a passenger";
      return "";
    }
    function validate() {
      const p = problems();
      setPrimary(p || "Save trip", save, !p);
    }
    function save() {
      const p = problems();
      if (p) { toast(p); return; }
      const trip = {
        id: src ? src.id : undefined, from: d.from, to: d.to, dates: d.dates, classes: d.classes,
        maxPrice: d.maxPrice || 0, mode: d.mode, passengers: d.mode === "buy" ? d.passengers : [],
        speed: d.speed, trains: d.trains, together: d.together, active: src ? src.active : true,
      };
      send({ a: "trip", trip }).then((live) => { if (live) { toast("Saved"); closingConfirm(false); tab = "trips"; tripsTab = "active"; render("home"); } });
    }
    validate();

    const initial = JSON.stringify(d);
    const dirty = () => JSON.stringify(d) !== initial;
    app.onclick = app.oninput = app.onchange = () => setTimeout(() => closingConfirm(dirty()), 0);
    setBack(() => dirty() ? confirmThen("Discard your changes to this trip?", () => render("home")) : render("home"));
  }

  // ------------------------------------------------------------------ account
  async function encrypt(text) {
    const der = Uint8Array.from(atob(S.key), (ch) => ch.charCodeAt(0));
    const key = await crypto.subtle.importKey("spki", der, { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"]);
    const out = await crypto.subtle.encrypt({ name: "RSA-OAEP" }, key, new TextEncoder().encode(text));
    return btoa(String.fromCharCode(...new Uint8Array(out)));
  }

  function renderAccount() {
    setBack(() => render("home"));
    app.innerHTML = `<div class="account"><h1 class="page-title">Railway account</h1>
        <p class="intro">Use the email and password from the railway ticket site. The bot signs in with them to hold seats for you.</p>
        <label for="email">Email</label>
        <div class="field"><input id="email" type="email" autocomplete="username" placeholder="you@example.com" value="${esc(S.email)}"></div>
        <label for="pw">Password</label>
        <div class="field"><input id="pw" type="password" autocomplete="current-password" placeholder="${S.hasPw ? "Saved, type to replace" : "Your eticket password"}">
          <button class="icon-btn" id="eye" aria-label="Show password">${ms("visibility")}</button></div>
        <div class="lock">${ms("lock", "fill")}<span>Your password is encrypted on this phone. It is only sent to the bot on your computer.</span></div></div>`;
    $("#eye").onclick = () => {
      const pw = $("#pw"), show = pw.type === "password";
      pw.type = show ? "text" : "password";
      $("#eye").innerHTML = ms(show ? "visibility_off" : "visibility");
      $("#eye").setAttribute("aria-label", show ? "Hide password" : "Show password");
    };
    const check = () => {
      const ok = /.+@.+\..+/.test($("#email").value.trim()) && $("#pw").value.length > 0;
      setPrimary("Save", saveAccount, ok);
    };
    $("#email").oninput = check;
    $("#pw").oninput = check;
    async function saveAccount() {
      if (!S.key) { toast("Open this page from your bot to save."); return; }
      try {
        const pw = await encrypt($("#pw").value);
        send({ a: "account", email: $("#email").value.trim(), pw });
      } catch (e) {
        toast("Couldn't encrypt the password on this device.");
      }
    }
    check();
  }

  // ------------------------------------------------------------------ start
  function applyTheme() {
    const dark = inTelegram ? tg.colorScheme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    if (inTelegram) {
      const bg = dark ? "#080C0A" : "#E6EBE7";
      try { tg.setHeaderColor(bg); tg.setBackgroundColor(bg); } catch (e) { /* older clients */ }
    }
  }
  applyTheme();
  if (inTelegram) {
    tg.ready();
    tg.expand();
    try { tg.onEvent("themeChanged", () => { applyTheme(); if (lastPrimary) setPrimary(...lastPrimary); }); } catch (e) { /* older clients */ }
  }
  render(location.hash.includes("v=account") ? "account" : location.hash.includes("v=new") ? "trip" : "home");
  setInterval(tick, 1000);
  if (S.live) { pull(); setInterval(pull, 2500); }
})();
