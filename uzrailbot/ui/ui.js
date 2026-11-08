/* UzRail Bot desktop window: talks to the local API of the bot running on this PC. */
(() => {
  "use strict";
  const KEY = new URLSearchParams(location.hash.slice(1)).get("k") || "";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  const CLASSES = {
    "3П": "Plaskart", "2К": "Kupe", "1Л": "SV", "1А": "Lux", "2Е": "Sitting economy",
    "1С": "Sitting business", "2В": "Sitting 2nd class", "1В": "Sitting 1st class",
  };
  const SHORT = { "3П": "Plaskart", "2К": "Kupe", "1Л": "SV", "1А": "Lux", "2Е": "Sitting eco", "1С": "Sitting biz", "2В": "Sitting 2nd", "1В": "Sitting 1st" };
  const cls = (c) => CLASSES[c] || c;
  const short = (c) => SHORT[c] || c;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const fmtDate = (d) => { const [y, m, day] = String(d).split("-"); return y && m ? `${+day} ${MONTHS[+m - 1]}` : d; };
  const fmtDay = (d) => { const [y, m, day] = String(d).split("-").map(Number); if (!y) return d;
    const w = new Date(Date.UTC(y, m - 1, day)).getUTCDay(); return `${DAYS[w]} ${day} ${MONTHS[m - 1]}`; };
  const hhmm = (stamp) => String(stamp || "").slice(-5);
  /* "07.11.2026 21:05" + "08.11.2026 07:40" -> "21:05 → 07:40 +1" */
  const span = (dep, arr) => {
    const d1 = String(dep || "").slice(0, 10), d2 = String(arr || "").slice(0, 10);
    return `${hhmm(dep)} → ${hhmm(arr)}${d1 && d2 && d1 !== d2 ? '<sup>+1</sup>' : ""}`;
  };
  const ago = (sec) => sec < 1 ? "just now" : sec < 60 ? `${sec.toFixed(sec < 10 ? 1 : 0)} s ago` : `${Math.round(sec / 60)} min ago`;
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
    $$(".nav").forEach((b) => { b.classList.toggle("on", b.dataset.view === v); b.dataset.view === v ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"); });
    $$(".page").forEach((p) => (p.hidden = p.dataset.page !== v));
    if (v === "activity") renderFeed();
  }
  $$(".nav").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

  document.addEventListener("click", (e) => {
    const o = e.target.closest("[data-open]");
    if (o) api("/api/open", { target: "url", url: o.dataset.open });
    const a = e.target.closest("[data-act]");
    if (a) api("/api/open", { target: a.dataset.act });
    if (e.target.closest("[data-console]")) {
      api("/api/console").then((r) => toast(r.ok ? "Live console opened in its own window" : r.error));
    }
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
    offline: { tone: "bad", title: "No connection to the railway site" },
  };

  function subline(s) {
    const active = s.trips.filter((t) => t.active);
    if (s.state === "watching") {
      const dates = new Set(active.flatMap((t) => t.dates));
      return `${active.length} trip${active.length === 1 ? "" : "s"} · ${dates.size} date${dates.size === 1 ? "" : "s"} · checking about every second`;
    }
    if (s.state === "paused") return "Nothing is checked until you resume.";
    if (s.state === "idle") return "Open the Telegram bot and add a trip in Trips & settings.";
    if (s.state === "setup") return s.account ? s.detail : "Connect your railway account in the Telegram bot (Trips & settings → Account).";
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
    $("#stAge").textContent = age == null ? "–" : ago(age);
    $("#stRate").textContent = s.checksPerMin ? s.checksPerMin : "–";
    $("#stMs").textContent = s.medianMs ? `${s.medianMs} ms` : "–";
    const st2 = s.stats || {};
    $("#stHolds").textContent = st2.holds != null ? st2.holds : "–";
    $("#stHoldsSub").textContent = st2.fastest ? `holds · fastest ${st2.fastest} s` : "holds this session";
    const pb = $("#pauseBtn");
    pb.innerHTML = s.paused ? '<svg><use href="#i-play"/></svg><span>Resume</span>' : '<svg><use href="#i-pause"/></svg><span>Pause</span>';
    // connection problems, said plainly
    const nb = $("#netBanner");
    const off = s.state === "offline" ? (s.offlineSince ? Date.now() / 1000 - s.offlineSince : 0) : null;
    if (off != null) {
      nb.hidden = false;
      nb.innerHTML = `<span class="spinner"></span><span><b>No answer from the railway site${off > 5 ? ` for ${ago(off).replace(" ago", "")}` : ""}.</b>
        Check this computer's internet. The bot keeps trying and carries on by itself.</span>`;
    } else if (s.state === "watching" && age != null && age > 20) {
      nb.hidden = false;
      nb.innerHTML = `<span class="spinner"></span><span>Checks are slow: the last answer came ${ago(age)}.</span>`;
    } else nb.hidden = true;
    renderHolds();
    renderBoard();
  }

  /* ---------------------------------------------------------------- live board */
  function classChip(c, v, trip) {
    const wanted = !trip.classes.length || trip.classes.includes(c);
    const over = trip.maxPrice && v.price > trip.maxPrice;
    let k = "c";
    if (!wanted) k += " other";
    else if (v.free > 0 && over) k += " over";
    else if (v.free > 0) k += " ok";
    else k += " zero";
    const price = v.free > 0 && v.price ? ` · ${fmtSum(v.price)}` : "";
    const title = wanted ? (over && v.free ? "Above your maximum price" : "") : "Not a class you chose";
    return `<span class="${k}" title="${title}">${esc(short(c))} <b>${v.free}</b>${price}</span>`;
  }
  function renderBoard() {
    const el = $("#board");
    const active = S.trips.filter((t) => t.active);
    $("#boardHead").hidden = !active.length || S.paused;
    if (!active.length || S.paused) { el.innerHTML = ""; return; }
    const now = Date.now() / 1000;
    const cards = [];
    for (const t of active) {
      for (const d of [...t.dates].sort().filter((x) => x >= S.today)) {
        const b = S.board.find((x) => x.from === t.from.code && x.to === t.to.code && x.date === d);
        let body;
        if (!b) body = '<p class="empty slim">Waiting for the first check…</p>';
        else if (!b.trains.length) body = '<p class="empty slim">No trains on this date.</p>';
        else {
          body = '<div class="trains">' + [...b.trains].sort((x, y) => hhmm(x.dep).localeCompare(hhmm(y.dep))).map((tr) => {
            const watched = !(t.trains && t.trains.length) || t.trains.includes(tr.number);
            const entries = Object.entries(tr.classes).sort(([a], [z]) =>
              (t.classes.includes(z) ? 1 : 0) - (t.classes.includes(a) ? 1 : 0));
            return `<div class="tr ${watched ? "" : "dim"}">
              <span class="num">${esc(tr.number)}</span>
              <span class="time">${span(tr.dep, tr.arr)}</span>
              <span class="cells">${entries.map(([c, v]) => classChip(c, v, t)).join("") || '<span class="c zero">sold out</span>'}
                ${watched ? "" : '<span class="muted small">not watched</span>'}</span>
            </div>`;
          }).join("") + "</div>";
        }
        const when = b ? `checked ${ago(now - b.at)}` : "";
        cards.push(`<div class="card board">
          <div class="board-head">
            <div><b>${esc(t.from.name)} → ${esc(t.to.name)}</b> <span class="muted">· ${esc(fmtDay(d))}</span></div>
            <span class="muted small">${when}</span>
          </div>${body}</div>`);
      }
    }
    el.innerHTML = cards.join("") || '<p class="empty">No upcoming dates in your active trips.</p>';
  }

  let holdsSig = "";
  function renderHolds() {
    const el = $("#holds");
    const sig = JSON.stringify([S.holds.map((h) => [h.order, h.seats, h.pay_url]), S.targets || {}]);
    if (sig !== holdsSig) {          // rebuild only when the holds change, so buttons stay clickable
      holdsSig = sig;
      const berth = { "В": "upper", "Н": "lower" };
      el.innerHTML = S.holds.map((h) => {
        const who = (h.names || []).map((n, i) => {
          const seat = (h.seats || [])[i] || "?";
          const b = berth[(h.seat_types || [])[i]];
          return `<li><span>${esc(n)}</span><b>seat ${esc(seat)}${b ? ` <em>${b}</em>` : ""}</b></li>`;
        }).join("");
        return `<div class="card hold">
        <div class="hold-top">
          <div><div class="hold-badge">Held for you · pay to keep it</div>
            <div class="hold-title">${esc(h.from)} → ${esc(h.to)}</div>
            <div class="muted small">${esc(fmtDay(h.date))} · ${h.dep ? span(h.dep, h.arr) + " · " : ""}train ${esc(h.train)}</div></div>
          <div class="timer-box"><div class="timer" data-order="${esc(h.order)}"></div><div class="muted small">left to pay</div></div>
        </div>
        <div class="kv">
          <div><span>Class</span><b>${esc(cls(h.cls))}</b></div>
          <div><span>Car</span><b>${esc(h.car)}</b></div>
          <div><span>Total</span><b>${fmtSum(h.total)} so'm</b></div>
          <div><span>If not paid</span><b>back on sale, searched again</b></div>
        </div>
        <ul class="seats-list">${who}</ul>
        <form class="xfer" data-xfer="${esc(h.order)}" hidden>
          <div class="xfer-row">
            <label>Seat of<select name="from">${(h.passengers || []).map((p, i) =>
              `<option value="${esc(p)}">${esc((h.names || [])[i] || "Passenger")} (seat ${esc((h.seats || [])[i] || "?")})</option>`).join("")}</select></label>
            <label>goes to<select name="to">${((S.targets || {})[h.order] || [])
              .map((p) => `<option value="${esc(p.id)}">${esc(p.name)} · ${p.pos ? `#${p.pos} in this trip` : "not in this trip"}</option>`).join("")
              || '<option value="">nobody else can take it (everyone has a seat or a ticket)</option>'}</select></label>
          </div>
          <p class="muted small">The reservation is cancelled and the same seat is re-booked for the new passenger at
            once. This usually works, because others see a freed seat only 2 to 9 s later. If it doesn't come back,
            another seat in the car or class is taken. In the trip, the new passenger takes the old one's place.
            ${(h.passengers || []).length > 1 ? "<b>This reservation has several passengers: all its seats are released and re-booked together, so seat numbers may change.</b>" : ""}</p>
          <div class="row-btns"><button type="button" class="btn ghost sm" data-xfer-close>Back</button>
            <button type="submit" class="btn primary sm">Move the seat</button></div>
        </form>
        <div class="row-btns">
          <button class="btn danger sm" data-cancel="${esc(h.order)}">Cancel</button>
          <button class="btn ghost sm" data-xfer-open="${esc(h.order)}">Transfer</button>
          ${h.pay_url ? `<button class="btn primary sm" data-open="${esc(h.pay_url)}">Pay ${fmtSum(h.total)} so'm</button>` : ""}
        </div>
      </div>`;
      }).join("");
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
  $("#holds").addEventListener("click", (e) => {
    const o = e.target.closest("[data-xfer-open]");
    if (o) { $(`form[data-xfer="${CSS.escape(o.dataset.xferOpen)}"]`).hidden = false; return; }
    const c = e.target.closest("[data-xfer-close]");
    if (c) c.closest("form").hidden = true;
  });
  $("#holds").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target.closest("form[data-xfer]");
    if (!f || !f.to.value) { toast("Choose who gets the seat"); return; }
    const btn = f.querySelector("[type=submit]");
    btn.disabled = true; btn.textContent = "Moving…";
    const r = await api("/api/transfer", { order: f.dataset.xfer, from: f.from.value, to: f.to.value });
    toast(r.ok ? (r.error ? `Moved. ${r.error}` : "Seat moved: the new reservation is in Telegram") : r.error);
    btn.disabled = false; btn.textContent = "Move the seat";
    refresh();
  });
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
    const sig = JSON.stringify([S.trips, S.holds.map((h) => [h.trip, h.passengers]), S.done, S.pax, S.today]);
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
      const future = dates.filter((d) => d >= S.today);
      const done = new Set((S.done || {})[t.id] || []);
      const held = new Set(S.holds.filter((h) => h.trip === t.id).flatMap((h) => h.passengers || []));
      const allDone = t.mode === "buy" && t.passengers.length && t.passengers.every((p) => done.has(p));
      const status = !t.active ? ["", "Paused"] : !future.length ? ["", "Dates passed"] : allDone ? ["ok", "Everyone has a ticket"]
        : held.size ? ["warn", "Seats held"] : ["ok", "Watching"];
      const pax = t.mode === "buy" ? t.passengers.map((p, i) => {
        const tag = done.has(p) ? '<em class="okc">ticket</em>' : held.has(p) ? '<em class="warnc">seat held</em>'
          : t.active && future.length ? '<em class="srch">searching</em>' : "";
        return `<li><span class="n">${i + 1}</span>${esc((S.pax || {})[p] || "Passenger")} ${tag}</li>`;
      }).join("") : "";
      const multi = t.mode === "buy" && t.passengers.length > 1;
      return `<div class="card trip ${t.active ? "" : "off"}">
        <div class="trip-top">
          <div class="trip-route">${esc(t.from.name)} <span class="arrow">→</span> ${esc(t.to.name)}</div>
          <span class="pill ${status[0]}">${status[1]}</span>
          <label class="switch" title="${t.active ? "Pause this trip" : "Resume this trip"}">
            <input type="checkbox" aria-label="Watch this trip" data-trip="${esc(t.id)}" ${t.active ? "checked" : ""}><span></span></label>
        </div>
        <div class="dates">${dates.map((d) => `<span class="date ${d < S.today ? "past" : ""}">${esc(fmtDay(d))}</span>`).join("")}</div>
        <div class="tags">
          <span class="tag g">${t.mode === "buy" ? "Auto-buy" : "Notify only"}</span>
          ${t.classes.map((c) => `<span class="tag">${esc(cls(c))}</span>`).join("") || '<span class="tag">Any class</span>'}
          <span class="tag">${t.maxPrice ? `up to ${fmtSum(t.maxPrice)} so'm` : "Any price"}</span>
          <span class="tag">${t.trains && t.trains.length ? `Only train ${esc(t.trains.join(", "))}` : "All trains"}</span>
          ${multi ? `<span class="tag">${t.together ? "Together in one car" : "May be split"}</span>` : ""}
        </div>
        ${pax ? `<ol class="pax">${pax}</ol>` : ""}
        <div class="trip-foot">
          <span class="muted small">Edit in Telegram: Trips &amp; settings</span>
          <button class="btn ghost sm" data-del="${esc(t.id)}">Delete</button>
        </div>
      </div>`;
    }).join("");
  }
  $("#trips").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-del]");
    if (!b || !confirm("Delete this trip? The bot stops watching it.")) return;
    await api("/api/trip/delete", { id: b.dataset.del });
    toast("Trip deleted");
    refresh();
  });
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
    state: (o) => (o.state === "watching" ? null : o.state === "offline" ? ["bad", "Lost the connection to the railway site"]
      : ["", `Status: ${o.state}${o.detail ? ` (${o.detail})` : ""}`]),
    trip_trains: (o) => ["", o.trains.length ? `Trip now watches only train ${o.trains.join(", ")}` : "Trip now watches all trains"],
    trip_deleted: () => ["", "Trip deleted"],
    availability: (o) => o.free > 0
      ? ["ok", `${o.free} seat${o.free === 1 ? "" : "s"} · ${cls(o.cls)} · train ${o.train} · ${fmtDate(o.date)} · ${fmtSum(o.price)} so'm`]
      : ["", `Seats sold out again on train ${o.train} (${cls(o.cls)})`],
    detect: (o) => ["ok", `New seats on ${fmtDate(o.date)}, booking now`],
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
    trip_saved: (o) => ["", `Trip saved: ${o.dates.length} date${o.dates.length === 1 ? "" : "s"}, ${o.classes.map(cls).join(", ")}`],
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

  async function pullActivity() {
    try {
      const r = await api("/api/activity", { after: lastTs });
      for (const row of r.rows) {
        lastTs = Math.max(lastTs, row.ts);
        const d = describe(row);
        if (!d) continue;
        const last = rows[rows.length - 1];
        if (last && last.text === d[1]) { last.n += 1; last.t = row.t; continue; }   // "Network problem ×6"
        rows.push({ t: row.t, kind: row.kind, tone: d[0], text: d[1], n: 1 });
      }
      if (rows.length > 400) rows.splice(0, rows.length - 400);
      if (r.rows.length && view === "activity") renderFeed();
    } catch (e) { /* the app may be closing */ }
  }

  const BOOKING = new Set(["availability", "detect", "fanout", "reserve_sent", "reserve_refused", "settled", "held",
    "extra_hold_cancelled", "seats_gone", "missed", "pay_link", "transfer_start", "transfer_attempt", "user_transfer", "hold_end", "paid", "user_cancel_hold",
    "recover_hold"]);
  let filter = "all";
  $("#filters").addEventListener("click", (e) => {
    const b = e.target.closest("[data-f]");
    if (!b) return;
    filter = b.dataset.f;
    $$("#filters .chip").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
    renderFeed();
  });
  function renderFeed() {
    const el = $("#feed");
    const list = rows.filter((r) => filter === "all" || (filter === "book" ? BOOKING.has(r.kind) : r.tone === "bad" || r.tone === "warn"));
    if (!list.length) {
      el.innerHTML = `<p class="empty">${filter === "bad" ? "No problems so far." : filter === "book" ? "No seats seen yet." : "Nothing yet."}</p>`;
      return;
    }
    el.innerHTML = list.slice().reverse().map((r) =>
      `<div class="ev ${r.tone}"><time>${esc(r.t.slice(0, 8))}</time><i></i><span>${esc(r.text)}${r.n > 1 ? ` <em class="x">×${r.n}</em>` : ""}</span></div>`).join("");
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
    $("#shareLogs").checked = S.shareLogs !== false;
  }
  $("#updBtn").addEventListener("click", async () => {
    await api("/api/update", { install: (S.update || {}).state === "available" });
    toast("Checking for updates…");
  });
  $("#autoUpdate").addEventListener("change", async (e) => {
    const r = await api("/api/autoupdate", { on: e.target.checked });
    toast(r.on ? "Updates install automatically" : "You'll be told about updates; install them from here");
  });
  $("#shareLogs").addEventListener("change", async (e) => {
    const r = await api("/api/sharelogs", { on: e.target.checked });
    toast(r.on ? "Usage logs are shared with the developer" : "Usage logs stay on this PC");
  });

  /* ---------------------------------------------------------------- report a problem */
  $("#reportBtn").addEventListener("click", () => { $("#reportForm").hidden = false; $("#reportText").focus(); });
  $("#reportCancel").addEventListener("click", () => { $("#reportForm").hidden = true; });
  $("#reportForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#reportSend").disabled = true;
    const r = await api("/api/report", { note: $("#reportText").value.trim() });
    $("#reportSend").disabled = false;
    if (r.ok) { $("#reportText").value = ""; $("#reportForm").hidden = true; toast("Report sent. Thank you!"); }
    else toast("Couldn't send the report. Check the internet connection and try again.");
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
    toast("Linked! Open the bot and tap Trips & settings.");
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
