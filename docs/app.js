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

  function toast(msg, ms = 2600) {
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, ms);
  }

  function send(payload) {
    const data = JSON.stringify(payload);
    if (data.length > 4000) { toast("Too much data for one save. Remove some dates."); return; }
    if (inTelegram) { tg.sendData(data); return; }       // closes the Mini App; the bot replies in the chat
    console.log("sendData", payload);
    toast("Preview mode: open this page from your bot to save.");
  }

  // ------------------------------------------------------------------ primary button (Telegram MainButton or our own)
  let primaryHandler = null;
  function setPrimary(text, handler, enabled = true) {
    primaryHandler = handler;
    if (inTelegram) {
      const mb = tg.MainButton;
      mb.setParams({ text, color: "#08bfa8", text_color: "#ffffff", is_active: enabled, is_visible: !!handler });
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
  fetch("timetable.json?v=1").then((r) => r.json()).then((t) => { TT = t; if (onTimetable) onTimetable(); })
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
                 nights: Math.max(0, s[j][3] - s[i][4]), fromDay: s[i][4], r: t.r || null });
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
  const runText = (r) => {
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
    if (view === "home") return renderHome();
    if (view === "trip") return renderTrip(arg);
    if (view === "account") return renderAccount();
  }

  function renderHome() {
    $("#title").textContent = "UzRail Bot";
    $("#subtitle").textContent = S.email ? S.email : "Ticket watcher";
    setBack(null);
    app.innerHTML = "";
    if (S.demo) {
      app.insertAdjacentHTML("beforeend", `<div class="banner">This is a preview. Open it from your bot's
        <b>&nbsp;Trips &amp; settings&nbsp;</b> button to see your own trips and save changes.</div>`);
    }
    app.appendChild($("#tpl-home").content.cloneNode(true));

    const acc = $("#accountCard");
    const connected = S.email && S.hasPw;
    acc.classList.toggle("warn", !connected);
    acc.innerHTML = `
      <div class="avatar">${connected ? esc(S.email[0].toUpperCase()) : "!"}</div>
      <div class="grow">
        <div class="label">Railway account</div>
        <div class="value">${connected ? esc(S.email) : "Not connected yet"}</div>
      </div>
      <button class="btn ${connected ? "" : "btn-soft"}" id="accBtn">${connected ? "Change" : "Connect"}</button>`;
    $("#accBtn").onclick = () => render("account");

    const trips = S.trips;
    $("#tripCount").textContent = trips.length ? `${trips.length}` : "";
    const list = $("#tripList");
    if (!trips.length) {
      list.innerHTML = `<div class="card empty">
        <div class="empty-icon"><svg viewBox="0 0 24 24" width="26" height="26"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></div>
        <strong>No trips yet</strong>
        <p>Add a route and dates. The bot watches every second and books the moment seats appear.</p></div>`;
    }
    for (const t of trips) {
      const future = (t.dates || []).filter((d) => d >= iso(today()));
      const el = document.createElement("article");
      el.className = "card trip";
      el.innerHTML = `
        <div class="trip-main">
          <div class="trip-route"><span class="status-dot ${t.active ? "" : "off"}"></span>
            ${esc(title(t.from.name))} <span class="arrow">→</span> ${esc(title(t.to.name))}</div>
          <div class="trip-meta">
            ${(future.length ? future : t.dates).slice(0, 6).map((d) => `<span class="tag">${fmtDay(d)}</span>`).join("")}
            ${future.length > 6 ? `<span class="tag">+${future.length - 6}</span>` : ""}
          </div>
          <div class="trip-meta">
            <span class="tag ${t.mode === "buy" ? "green" : ""}">${t.mode === "buy" ? "Auto-buy" : "Notify only"}</span>
            ${(t.classes.length ? t.classes : ["any"]).map((c) => `<span class="tag">${c === "any" ? "Any class" : esc(shortClass(c))}</span>`).join("")}
            ${t.maxPrice ? `<span class="tag">≤ ${fmtSum(t.maxPrice)} so'm</span>` : ""}
            ${t.trains && t.trains.length ? `<span class="tag">Only ${esc(t.trains.join(", "))}</span>` : ""}
            ${t.active ? "" : `<span class="tag amber">Paused</span>`}
          </div>
          ${t.mode === "buy" ? `<div class="trip-people">${t.passengers.map((p, i) => `${i + 1}. ${esc(paxName(p))}`).join(" · ")}</div>` : ""}
        </div>
        <div class="trip-actions">
          <button data-act="edit">Edit</button>
          <button data-act="toggle">${t.active ? "Pause" : "Resume"}</button>
          <button data-act="del" class="danger">Delete</button>
        </div>`;
      el.querySelector('[data-act="edit"]').onclick = () => render("trip", t.id);
      el.querySelector('[data-act="toggle"]').onclick = () => send({ a: "toggle", id: t.id, active: !t.active });
      el.querySelector('[data-act="del"]').onclick = () => {
        const go = () => send({ a: "del", id: t.id });
        if (inTelegram && tg.showConfirm) tg.showConfirm("Delete this trip?", (ok) => ok && go());
        else if (confirm("Delete this trip?")) go();
      };
      list.appendChild(el);
    }
    setPrimary("New trip", () => render("trip", null));
  }

  // ------------------------------------------------------------------ trip editor
  function renderTrip(id) {
    const src = id ? S.trips.find((t) => t.id === id) : null;
    const d = src ? JSON.parse(JSON.stringify(src)) : {
      from: null, to: null, dates: [], classes: [], maxPrice: 0, mode: "buy",
      passengers: [], speed: 1, trains: [], together: false, active: true,
    };
    d.trains = d.trains || [];
    d.dates = (d.dates || []).filter((x) => x >= iso(today()));
    let month = d.dates.length ? new Date(d.dates[0] + "T00:00:00") : today();
    month = new Date(month.getFullYear(), month.getMonth(), 1);

    $("#title").textContent = src ? "Edit trip" : "New trip";
    $("#subtitle").textContent = "Route, dates and who travels";
    setBack(() => render("home"));

    app.innerHTML = `
      <section class="card form-section">
        <h3>Route</h3>
        <div class="route">
          <div class="fields"><div id="fromBox"></div><div id="toBox"></div></div>
          <button class="swap" id="swap" aria-label="Swap stations">
            <svg viewBox="0 0 24 24" width="20" height="20"><path d="M7 4v14m0 0l-3-3m3 3l3-3M17 20V6m0 0l-3 3m3-3l3 3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
      </section>

      <section class="card form-section">
        <h3>Dates</h3>
        <div class="cal-head">
          <button class="icon-btn" id="prevM" aria-label="Previous month"><svg viewBox="0 0 24 24" width="20" height="20"><path d="M15 5l-7 7 7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>
          <strong id="monthName"></strong>
          <button class="icon-btn" id="nextM" aria-label="Next month"><svg viewBox="0 0 24 24" width="20" height="20"><path d="M9 5l7 7-7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>
        </div>
        <div class="cal" id="cal"></div>
        <div class="chosen" id="chosen"></div>
      </section>

      <section class="card form-section">
        <h3>Trains on this route <span class="muted small" id="trainCount"></span></h3>
        <div id="trainList"></div>
        <p class="hint" id="trainHint"></p>
      </section>

      <section class="card form-section">
        <h3>Ticket classes</h3>
        <div class="chips" id="classes"></div>
        <details class="more-classes" id="otherClasses" hidden>
          <summary>Other classes <span class="muted" id="otherCount"></span></summary>
          <div class="chips" id="classesOther"></div>
        </details>
        <p class="hint" id="classHint"></p>
        <div class="field" style="margin-top:14px">
          <label for="maxPrice">Maximum price per ticket (optional)</label>
          <input class="input" id="maxPrice" inputmode="numeric" placeholder="No limit">
        </div>
      </section>

      <section class="card form-section">
        <h3>When seats appear</h3>
        <div class="seg" id="mode">
          <button data-v="buy">Auto-buy</button>
          <button data-v="notify">Notify only</button>
        </div>
        <p class="hint" id="modeHint"></p>
      </section>

      <section class="card form-section" id="paxSection">
        <h3>Passengers</h3>
        <div class="pax" id="pax"></div>
        <p class="hint">Tap to choose (up to ${MAX_PAX}). The order is the priority: if fewer seats appear, the first ones get them.</p>
        <div class="divider"></div>
        <label class="switch-row">
          <span class="txt">Keep everyone together<small>On: book only when all passengers fit in one car. Off: book whoever fits first, so people may end up in different cars, classes or trains.</small></span>
          <span class="switch"><input type="checkbox" id="together"><span></span></span>
        </label>
      </section>

      <section class="card form-section">
        <details class="more" id="more">
          <summary>Advanced <span class="muted" id="moreSum"></span></summary>
          <div class="field">
            <label>Check speed</label>
            <div class="seg" id="speed"></div>
          </div>
        </details>
      </section>`;

    // stations
    function stationBox(key, label) {
      const box = $(key === "from" ? "#fromBox" : "#toBox");
      if (d[key]) {
        box.innerHTML = `<div class="picked"><span>${esc(title(d[key].name))}<small>${label}</small></span>
          <button class="icon-btn" aria-label="Change ${label}"><svg viewBox="0 0 24 24" width="18" height="18"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button></div>`;
        box.querySelector("button").onclick = () => { d[key] = null; stationBox(key, label); box.querySelector("input").focus(); validate(); refreshRoute(); };
        return;
      }
      box.innerHTML = `<input class="input" placeholder="${label}: type a station" autocomplete="off"><div class="suggest" hidden></div>`;
      const input = box.querySelector("input"), sug = box.querySelector(".suggest");
      input.addEventListener("input", () => {
        const found = searchStations(input.value);
        sug.hidden = !found.length;
        sug.innerHTML = found.map((s) => `<button data-c="${esc(s.c)}"><span>${esc(title(s.en))}</span><small>${esc(s.uz && norm(s.uz) !== norm(s.en) ? title(s.uz) : (s.uzb ? "" : "abroad"))}</small></button>`).join("");
        sug.querySelectorAll("button").forEach((b) => {
          b.onclick = () => {
            const s = STATIONS.find((x) => x.c === b.dataset.c);
            const other = key === "from" ? d.to : d.from;
            if (other && other.code === s.c) { toast("Pick a different station"); return; }
            d[key] = { code: s.c, name: s.en };
            haptic();
            stationBox(key, label);
            validate();
            refreshRoute();
          };
        });
      });
    }
    stationBox("from", "From");
    stationBox("to", "To");
    $("#swap").onclick = () => { [d.from, d.to] = [d.to, d.from]; stationBox("from", "From"); stationBox("to", "To"); haptic(); validate(); refreshRoute(); };

    // calendar
    const DOW = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
    function drawCal() {
      $("#monthName").textContent = month.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
      const first = (month.getDay() + 6) % 7;
      const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
      const t0 = today(), last = new Date(t0); last.setDate(last.getDate() + 90);
      let html = DOW.map((x) => `<div class="dow">${x}</div>`).join("");
      for (let i = 0; i < first; i++) html += "<div></div>";
      for (let day = 1; day <= days; day++) {
        const dt = new Date(month.getFullYear(), month.getMonth(), day);
        const k = iso(dt);
        const off = dt < t0 || dt > last;
        const cls = ["day", d.dates.includes(k) ? "on" : "", k === iso(t0) ? "today" : ""].join(" ");
        html += `<button class="${cls}" data-d="${k}" ${off ? "disabled" : ""}>${day}</button>`;
      }
      $("#cal").innerHTML = html;
      $("#cal").querySelectorAll(".day:not(:disabled)").forEach((b) => {
        b.onclick = () => {
          const k = b.dataset.d;
          d.dates = d.dates.includes(k) ? d.dates.filter((x) => x !== k) : [...d.dates, k].sort();
          haptic(); drawCal(); validate(); refreshRoute();
        };
      });
      $("#chosen").innerHTML = d.dates.map((x) => `<span class="tag green">${fmtDay(x)}</span>`).join("") ||
        `<span class="muted">Pick one or more dates.</span>`;
      const prev = new Date(month.getFullYear(), month.getMonth(), 0);
      $("#prevM").disabled = prev < new Date(t0.getFullYear(), t0.getMonth(), 1);
    }
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
      const chip = (k) => `<button class="chip ${d.classes.includes(k.c) ? "on" : ""}" data-c="${k.c}">
        <span class="check">✓</span>${esc(k.name)}<span class="muted">${esc(k.sub)}${count[k.c] ? ` · ${count[k.c]} train${count[k.c] === 1 ? "" : "s"}` : ""}</span></button>`;
      $("#classes").innerHTML = main.map(chip).join("");
      $("#classesOther").innerHTML = rest.map(chip).join("");
      $("#otherClasses").hidden = !rest.length;
      $("#otherCount").textContent = rest.length ? `(${rest.length}, not seen on this route)` : "";
      if (rest.some((k) => d.classes.includes(k.c))) $("#otherClasses").open = true;
      document.querySelectorAll("#classes .chip, #classesOther .chip").forEach((b) => {
        b.onclick = () => {
          const c = b.dataset.c;
          d.classes = d.classes.includes(c) ? d.classes.filter((x) => x !== c) : [...d.classes, c];
          haptic(); drawClasses();
        };
      });
      const what = !d.from || !d.to ? "Pick the route to see which classes its trains have. "
        : !TT ? "Loading the timetable… "
        : known ? `Classes on ${d.trains.length ? "your chosen trains" : "this route"}. ` : "";
      $("#classHint").textContent = what + (d.classes.length ? `${d.classes.length} selected.` : "Nothing selected means any class.");
    }
    drawClasses();
    const mp = $("#maxPrice");
    mp.value = d.maxPrice ? fmtSum(d.maxPrice) : "";
    mp.oninput = () => { const v = mp.value.replace(/\D/g, ""); d.maxPrice = v ? parseInt(v, 10) : 0; };
    mp.onblur = () => { mp.value = d.maxPrice ? fmtSum(d.maxPrice) : ""; };

    // mode
    function drawMode() {
      $("#mode").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.v === d.mode));
      $("#modeHint").textContent = d.mode === "buy"
        ? "Holds the seats at once and sends you a Pay button (12 minutes to pay)."
        : "Just sends you a message with the free seats.";
      $("#paxSection").hidden = d.mode !== "buy";
      validate();
    }
    $("#mode").querySelectorAll("button").forEach((b) => { b.onclick = () => { d.mode = b.dataset.v; haptic(); drawMode(); }; });

    // passengers
    function drawPax() {
      const chosen = d.passengers.filter((p) => S.pax.some((x) => x.i === p));
      d.passengers = chosen;
      const rows = [...chosen.map((id) => S.pax.find((x) => x.i === id)), ...S.pax.filter((x) => !chosen.includes(x.i))];
      if (!S.pax.length) {
        $("#pax").innerHTML = S.email && S.hasPw
          ? `<div class="muted">Your saved passengers aren't on this page yet. Close it, send
             <b>/start</b> in the chat, then tap <b>🚆 Trips &amp; settings</b> again (that loads a fresh list).<br>
             No passengers saved? Add them on eticket.uzrailpass.uz → <b>My fellow travelers</b>.</div>`
          : `<div class="muted">Connect your railway account first (the <b>Railway account</b> card on the main
             page). Your saved passengers ("My fellow travelers" on eticket.uzrailpass.uz) then appear here.</div>`;
        return;
      }
      $("#pax").innerHTML = rows.map((p) => {
        const idx = chosen.indexOf(p.i);
        const on = idx >= 0;
        return `<div class="pax-row ${on ? "on" : ""}" data-i="${esc(p.i)}">
          <span class="order">${on ? idx + 1 : ""}</span><span class="name">${esc(p.n)}</span>
          ${on ? `<button class="mini" data-up ${idx === 0 ? "disabled" : ""} aria-label="Move up">▲</button>
                  <button class="mini" data-down ${idx === chosen.length - 1 ? "disabled" : ""} aria-label="Move down">▼</button>` : ""}
        </div>`;
      }).join("");
      $("#pax").querySelectorAll(".pax-row").forEach((row) => {
        const id = row.dataset.i;
        row.onclick = (e) => {
          if (e.target.closest("[data-up],[data-down]")) return;
          if (d.passengers.includes(id)) d.passengers = d.passengers.filter((x) => x !== id);
          else if (d.passengers.length < MAX_PAX) d.passengers.push(id);
          else { toast(`Up to ${MAX_PAX} passengers per booking`); return; }
          haptic(); drawPax(); validate();
        };
        const move = (dir) => {
          const i = d.passengers.indexOf(id), j = i + dir;
          [d.passengers[i], d.passengers[j]] = [d.passengers[j], d.passengers[i]];
          haptic(); drawPax();
        };
        const up = row.querySelector("[data-up]"), down = row.querySelector("[data-down]");
        if (up) up.onclick = () => move(-1);
        if (down) down.onclick = () => move(1);
      });
    }
    drawPax();
    $("#together").checked = !!d.together;
    $("#together").onchange = (e) => { d.together = e.target.checked; };

    // advanced
    $("#speed").innerHTML = SPEEDS.map((s) => `<button data-v="${s.v}">${s.label}<br><span class="muted">${s.sub}</span></button>`).join("");
    function drawSpeed() {
      $("#speed").querySelectorAll("button").forEach((b) => { b.classList.toggle("on", +b.dataset.v === d.speed); b.style.height = "52px"; b.style.lineHeight = "1.2"; });
      $("#moreSum").textContent = `${SPEEDS.find((s) => s.v === d.speed).label} speed`;
    }
    $("#speed").querySelectorAll("button").forEach((b) => { b.onclick = () => { d.speed = +b.dataset.v; haptic(); drawSpeed(); }; });
    drawSpeed();

    // trains: every train by default; tap some to watch only those
    let routeKey = d.from && d.to ? d.from.code + ">" + d.to.code : "";
    function drawTrains() {
      const key = d.from && d.to ? d.from.code + ">" + d.to.code : "";
      if (key !== routeKey) { d.trains = []; routeKey = key; }          // a new route: back to all trains
      const list = $("#trainList");
      if (!key) {
        list.innerHTML = `<p class="placeholder">Pick <b>From</b> and <b>To</b> to see the trains on this route, their times and classes.</p>`;
        $("#trainCount").textContent = ""; $("#trainHint").textContent = "";
        return;
      }
      if (!TT) { list.innerHTML = `<p class="placeholder">Loading the timetable…</p>`; return; }
      const rows = routeTrains(d.from.code, d.to.code);
      const sameRoute = src && src.from.code === d.from.code && src.to.code === d.to.code;
      for (const [n, dep, arr] of (sameRoute && src.kt) || []) {         // trains the PC saw that the timetable lacks
        if (!rows.some((r) => r.n === n)) rows.push({ n, b: "", c: [], dep, arr, nights: 0, fromDay: 0, r: null });
      }
      for (const n of d.trains) if (!rows.some((r) => r.n === n)) rows.push({ n, b: "", c: [], dep: "", arr: "", nights: 0, r: null });
      rows.sort((x, y) => (x.dep || "99").localeCompare(y.dep || "99"));
      $("#trainCount").textContent = rows.length ? `· ${rows.length}` : "";
      if (!rows.length) {
        list.innerHTML = `<p class="placeholder">No trains for this route in the timetable. Every train is watched, and the bot
          shows the live list after you save.</p>`;
        $("#trainHint").textContent = "";
        return;
      }
      const all = !d.trains.length;
      list.innerHTML = `<button class="train-row all ${all ? "on" : ""}" data-t="*"><span class="tick">✓</span>
          <span class="t-main"><b>All trains</b><small>Watch every train on the route (recommended)</small></span></button>` +
        rows.map((t) => {
          const runs = d.dates.map((x) => runsOn(t, x));
          const no = d.dates.length && runs.every((x) => x === false);
          const some = d.dates.length && !no && runs.some((x) => x === false);
          const note = no ? `<em class="warn">doesn't run on your ${d.dates.length === 1 ? "date" : "dates"}</em>`
            : some ? `<em>runs on ${runs.filter((x) => x !== false).length} of ${d.dates.length} dates</em>`
            : runText(t.r) ? `<em>${esc(runText(t.r))}</em>` : "";
          const cls = t.c.map((c) => `<i>${esc(shortClass(c))}</i>`).join("");
          return `<button class="train-row ${d.trains.includes(t.n) ? "on" : ""} ${no ? "dim" : ""}" data-t="${esc(t.n)}">
            <span class="tick">✓</span>
            <span class="t-main"><b>${esc(t.n)}</b><small>${esc(t.b)}</small></span>
            <span class="t-time">${esc(t.dep || "")}${t.dep ? " → " : ""}${esc(t.arr || "")}${t.nights ? `<sup>+${t.nights}</sup>` : ""}</span>
            <span class="t-cls">${cls}${note}</span>
          </button>`;
        }).join("");
      $("#trainHint").textContent = all
        ? "From the railway timetable. Live seats are checked after you save."
        : `Watching only ${d.trains.join(", ")}. Tap All trains to watch every train again.`;
      list.querySelectorAll("[data-t]").forEach((b) => {
        b.onclick = () => {
          const n = b.dataset.t;
          if (n === "*") d.trains = [];
          else d.trains = d.trains.includes(n) ? d.trains.filter((x) => x !== n) : [...d.trains, n];
          haptic(); drawTrains(); drawClasses();
        };
      });
    }
    function refreshRoute() { drawTrains(); drawClasses(); }
    onTimetable = () => { if (document.getElementById("trainList")) refreshRoute(); };
    drawTrains();
    drawClasses();
    drawMode();

    function problems() {
      if (!d.from || !d.to) return "Choose both stations";
      if (!d.dates.length) return "Pick at least one date";
      if (d.mode === "buy" && !d.passengers.length) return "Choose at least one passenger";
      return "";
    }
    function validate() {
      const p = problems();
      setPrimary(p || (src ? "Save changes" : "Start watching"), save, !p);
    }
    function save() {
      const p = problems();
      if (p) { toast(p); return; }
      const trip = {
        id: src ? src.id : undefined, from: d.from, to: d.to, dates: d.dates, classes: d.classes,
        maxPrice: d.maxPrice || 0, mode: d.mode, passengers: d.mode === "buy" ? d.passengers : [],
        speed: d.speed, trains: d.trains, together: d.together, active: src ? src.active : true,
      };
      send({ a: "trip", trip });
    }
    validate();
  }

  // ------------------------------------------------------------------ account
  async function encrypt(text) {
    const der = Uint8Array.from(atob(S.key), (ch) => ch.charCodeAt(0));
    const key = await crypto.subtle.importKey("spki", der, { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"]);
    const out = await crypto.subtle.encrypt({ name: "RSA-OAEP" }, key, new TextEncoder().encode(text));
    return btoa(String.fromCharCode(...new Uint8Array(out)));
  }

  function renderAccount() {
    $("#title").textContent = "Railway account";
    $("#subtitle").textContent = "eticket.uzrailpass.uz login";
    setBack(() => render("home"));
    app.innerHTML = `
      <section class="card">
        <div class="field">
          <label for="email">Email</label>
          <input class="input" id="email" type="email" autocomplete="username" placeholder="you@example.com" value="${esc(S.email)}">
        </div>
        <div class="field">
          <label for="pw">Password</label>
          <div class="input-wrap">
            <input class="input" id="pw" type="password" autocomplete="current-password" placeholder="${S.hasPw ? "Saved, type to replace" : "Your eticket password"}">
            <button class="inside" id="eye" aria-label="Show password"><svg viewBox="0 0 24 24" width="20" height="20"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="1.8"/></svg></button>
          </div>
        </div>
        <div class="lock-note">
          <svg viewBox="0 0 24 24" width="16" height="16"><path d="M7 11V8a5 5 0 0110 0v3M6 11h12v10H6z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>
          <span>Your password is encrypted on this phone with your PC's key before it is sent, and stored encrypted on your PC only. Passengers come from your saved "fellow travelers" on the railway site.</span>
        </div>
      </section>`;
    $("#eye").onclick = () => { const pw = $("#pw"); pw.type = pw.type === "password" ? "text" : "password"; };
    const check = () => {
      const ok = /.+@.+\..+/.test($("#email").value.trim()) && $("#pw").value.length > 0;
      setPrimary("Save and connect", saveAccount, ok);
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
      try { tg.setHeaderColor(dark ? "#182029" : "#ffffff"); tg.setBackgroundColor(dark ? "#0f141b" : "#f3f6fa"); }
      catch (e) { /* older clients */ }
    }
  }
  applyTheme();
  if (inTelegram) {
    tg.ready();
    tg.expand();
    try { tg.onEvent("themeChanged", applyTheme); } catch (e) { /* older clients */ }
  }
  render(location.hash.includes("v=account") ? "account" : location.hash.includes("v=new") ? "trip" : "home");
})();
