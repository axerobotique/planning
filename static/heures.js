(function () {
  "use strict";

  const CFG = window.HEURES_CONFIG;
  const MOIS = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"];
  const JOURS = ["Dim", "Lun", "Mar", "Mer", "Jeu", "Ven", "Sam"];
  const FIELDS = ["absence", "arrivee", "debut_dej", "fin_dej", "depart", "affaire_matin", "lieu_matin", "affaire_aprem", "lieu_aprem", "vehicule", "covoiturage"];
  const TIME_FIELDS = ["arrivee", "debut_dej", "fin_dej", "depart"];
  // Horaires types remplis par le bouton ↻ (cf. référence 7,75 h lun-jeu / 4 h ven).
  const STANDARD = { arrivee: "08:00", debut_dej: "12:00", fin_dej: "13:15", depart: "17:00" };
  const STANDARD_FRIDAY = { arrivee: "08:00", debut_dej: "", fin_dej: "", depart: "12:00" };
  const SAVE_DELAY_MS = 700;

  const weeksRoot = document.getElementById("weeks-root");
  const kpis = document.getElementById("kpis");
  const bannerRoot = document.getElementById("banner-root");
  const monthLabel = document.getElementById("month-label");
  const showWeekends = document.getElementById("show-weekends");

  const today = new Date();
  let year = today.getFullYear();
  let month = today.getMonth();
  const byYear = {}; // année -> { iso: champs }
  const saveTimers = {};

  try { showWeekends.checked = localStorage.getItem("heures-weekends") === "1"; } catch (e) { /* stockage indisponible */ }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function showToast(text, kind) {
    const el = document.createElement("div");
    el.className = "toast toast-" + (kind || "success");
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  async function fetchJSON(url, opts, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || 20000);
    try {
      const resp = await fetch(url, { ...(opts || {}), signal: controller.signal });
      return await resp.json();
    } catch (e) {
      if (e.name === "AbortError") throw new Error("Délai dépassé — le serveur ne répond pas.");
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  function iso(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  function isoWeek(d) {
    const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    const day = t.getUTCDay() || 7;
    t.setUTCDate(t.getUTCDate() + 4 - day);
    const yearStart = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
    return Math.ceil(((t - yearStart) / 86400000 + 1) / 7);
  }

  function minutes(hhmm) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm || "");
    return m ? Number(m[1]) * 60 + Number(m[2]) : null;
  }

  function refHours(d) {
    return CFG.ref_by_weekday[(d.getDay() + 6) % 7];
  }

  // Miroir de `heures_totals` (app.py) : total travaillé et écart à la référence.
  function computeDay(d, e) {
    const ref = refHours(d);
    if (!e) return { total: null, ecart: null, ref };
    if (e.absence) return { total: ref, ecart: 0, ref };
    const start = minutes(e.arrivee), end = minutes(e.depart);
    if (start == null || end == null) return { total: null, ecart: null, ref };
    let total = end - start;
    const ls = minutes(e.debut_dej), le = minutes(e.fin_dej);
    if (ls != null && le != null) total -= Math.max(0, le - ls);
    const hours = Math.round(total / 60 * 100) / 100;
    return { total: hours, ecart: Math.round((hours - ref) * 100) / 100, ref };
  }

  function fmt(h, signed) {
    if (h == null) return "";
    const s = h.toFixed(2).replace(".", ",");
    return signed && h > 0 ? "+" + s : s;
  }

  function ecartClass(v) {
    if (v == null || v === 0) return "";
    return v > 0 ? "pos" : "neg";
  }

  function entries() {
    return byYear[year] || {};
  }

  function daysOfMonth() {
    const out = [];
    const d = new Date(year, month, 1);
    while (d.getMonth() === month) {
      out.push(new Date(d));
      d.setDate(d.getDate() + 1);
    }
    return out;
  }

  function isVisible(d) {
    const wd = d.getDay();
    return showWeekends.checked || (wd !== 0 && wd !== 6) || !!entries()[iso(d)];
  }

  async function load() {
    monthLabel.textContent = `${MOIS[month]} ${year}`;
    bannerRoot.innerHTML = "";
    if (!byYear[year]) {
      weeksRoot.innerHTML = '<p class="loading">Chargement…</p>';
      try {
        const d = await fetchJSON(`/api/heures?employee=${encodeURIComponent(CFG.employee)}&year=${year}`);
        if (!d.ok) throw new Error(d.error || "Erreur.");
        byYear[year] = d.entries;
      } catch (e) {
        weeksRoot.innerHTML = "";
        bannerRoot.innerHTML = `<div class="banner banner-error">${esc(e.message)}</div>`;
        return;
      }
    }
    render();
  }

  function cellInput(field, value, attrs) {
    return `<input data-f="${field}" value="${esc(value)}" ${attrs}>`;
  }

  function rowHtml(d) {
    const key = iso(d);
    const e = entries()[key] || {};
    const c = computeDay(d, entries()[key]);
    const isToday = key === iso(today);
    const absent = !!e.absence;
    const absOpts = ['<option value="">—</option>']
      .concat(CFG.absences.map((a) => `<option ${a === e.absence ? "selected" : ""}>${esc(a)}</option>`)).join("");
    const coOpts = ['<option value="">—</option>']
      .concat(CFG.colleagues.map((n) => `<option ${n === e.covoiturage ? "selected" : ""}>${esc(n)}</option>`)).join("");
    const t = (f, label) => `<label class="hr-f hr-f-time"><span class="hr-lbl">${label}</span>${cellInput(f, e[f], `type="time" ${absent ? "disabled" : ""}`)}</label>`;
    const txt = (f, label, list) => `<label class="hr-f hr-f-txt"><span class="hr-lbl">${label}</span>${cellInput(f, e[f], `type="text" list="${list}" autocomplete="off" class="hr-upper"`)}</label>`;
    return `
      <div class="hr-row${isToday ? " is-today" : ""}${absent ? " is-absent" : ""}" data-iso="${key}">
        <div class="hr-date"><span class="hr-dow">${JOURS[d.getDay()]}</span> ${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}</div>
        <label class="hr-f"><span class="hr-lbl">Absence</span><select data-f="absence">${absOpts}</select></label>
        ${t("arrivee", "Arrivée")}
        ${t("debut_dej", "Début déj.")}
        ${t("fin_dej", "Fin déj.")}
        ${t("depart", "Départ")}
        <div class="hr-total"><span class="hr-lbl">Total</span><span data-out="total">${fmt(c.total)}</span></div>
        <div class="hr-ecart ${ecartClass(c.ecart)}"><span class="hr-lbl">Écart</span><span data-out="ecart">${fmt(c.ecart, true)}</span></div>
        ${txt("affaire_matin", "Affaire matin", "dl-affaires")}
        ${txt("lieu_matin", "Lieu matin", "dl-lieux")}
        ${txt("affaire_aprem", "Affaire après-midi", "dl-affaires")}
        ${txt("lieu_aprem", "Lieu après-midi", "dl-lieux")}
        <label class="hr-f hr-f-txt"><span class="hr-lbl">Véhicule</span>${cellInput("vehicule", e.vehicule, 'type="text" list="dl-vehicules" autocomplete="off"')}</label>
        <label class="hr-f"><span class="hr-lbl">Covoiturage avec</span><select data-f="covoiturage">${coOpts}</select></label>
        <div class="hr-actions">
          <button type="button" class="hr-fill" title="Remplir les horaires types" ${absent ? "disabled" : ""}>&#8635;</button>
          <span class="hr-status" title=""></span>
        </div>
      </div>`;
  }

  function render() {
    const weeks = [];
    for (const d of daysOfMonth()) {
      if (!isVisible(d)) continue;
      const w = isoWeek(d);
      if (!weeks.length || weeks[weeks.length - 1].num !== w) weeks.push({ num: w, days: [] });
      weeks[weeks.length - 1].days.push(d);
    }
    const head = `
      <div class="hr-row hr-head">
        <div>Date</div><div>Absence</div><div>Arrivée</div><div>Début déj.</div><div>Fin déj.</div><div>Départ</div>
        <div>Total</div><div>Écart</div><div>Affaire matin</div><div>Lieu matin</div><div>Affaire après-midi</div>
        <div>Lieu après-midi</div><div>Véhicule</div><div>Covoiturage</div><div></div>
      </div>`;
    weeksRoot.innerHTML = weeks.map((w) => `
      <section class="hr-week" data-week="${w.num}">
        <h2 class="hr-week-title">Semaine n° ${w.num} <span class="hr-week-sum"></span></h2>
        <div class="hr-grid">${head}${w.days.map(rowHtml).join("")}</div>
      </section>`).join("") || '<p class="hr-empty">Aucun jour à afficher.</p>';
    weeksRoot.querySelectorAll(".hr-row[data-iso]").forEach(bindRow);
    renderSummaries();
  }

  function sumDays(days) {
    let total = 0, ref = 0, ecart = 0, filled = 0;
    for (const d of days) {
      const e = entries()[iso(d)];
      const c = computeDay(d, e);
      if (c.total == null) continue;
      filled++;
      total += c.total;
      ref += c.ref;
      ecart += c.ecart;
    }
    return { total, ref, ecart: Math.round(ecart * 100) / 100, filled };
  }

  function renderSummaries() {
    weeksRoot.querySelectorAll(".hr-week").forEach((sec) => {
      const days = [...sec.querySelectorAll(".hr-row[data-iso]")].map((r) => parseIsoLocal(r.dataset.iso));
      const s = sumDays(days);
      sec.querySelector(".hr-week-sum").innerHTML =
        `— ${fmt(s.total)} h <span class="${ecartClass(s.ecart)}">(${fmt(s.ecart, true)})</span>`;
    });

    const month_ = sumDays(daysOfMonth());
    const yearDays = Object.keys(entries()).map(parseIsoLocal);
    const yearSum = sumDays(yearDays);
    const missing = daysOfMonth().filter((d) => iso(d) < iso(today) && refHours(d) > 0 && computeDay(d, entries()[iso(d)]).total == null).length;
    kpis.innerHTML = `
      <div class="hr-kpi"><span class="hr-kpi-v">${fmt(month_.total)}</span><span class="hr-kpi-l">Heures du mois</span></div>
      <div class="hr-kpi"><span class="hr-kpi-v">${fmt(month_.ref)}</span><span class="hr-kpi-l">Référence (jours saisis)</span></div>
      <div class="hr-kpi"><span class="hr-kpi-v ${ecartClass(month_.ecart)}">${fmt(month_.ecart, true)}</span><span class="hr-kpi-l">Écart du mois</span></div>
      <div class="hr-kpi"><span class="hr-kpi-v ${ecartClass(yearSum.ecart)}">${fmt(yearSum.ecart, true)}</span><span class="hr-kpi-l">Cumul écart ${year}</span></div>
      <div class="hr-kpi${missing ? " is-warn" : ""}"><span class="hr-kpi-v">${missing}</span><span class="hr-kpi-l">Jour(s) passé(s) non saisi(s)</span></div>`;
  }

  function parseIsoLocal(s) {
    const [y, m, d] = s.split("-").map(Number);
    return new Date(y, m - 1, d);
  }

  function readRow(row) {
    const out = {};
    for (const f of FIELDS) {
      const el = row.querySelector(`[data-f="${f}"]`);
      out[f] = el ? el.value.trim() : "";
    }
    return out;
  }

  function refreshRow(row) {
    const d = parseIsoLocal(row.dataset.iso);
    const fields = readRow(row);
    const absent = !!fields.absence;
    row.classList.toggle("is-absent", absent);
    TIME_FIELDS.forEach((f) => { row.querySelector(`[data-f="${f}"]`).disabled = absent; });
    row.querySelector(".hr-fill").disabled = absent;
    const c = computeDay(d, fields);
    row.querySelector('[data-out="total"]').textContent = fmt(c.total);
    const ec = row.querySelector(".hr-ecart");
    ec.className = "hr-ecart " + ecartClass(c.ecart);
    ec.querySelector('[data-out="ecart"]').textContent = fmt(c.ecart, true);
  }

  function setStatus(row, kind, title) {
    const st = row.querySelector(".hr-status");
    st.className = "hr-status " + (kind ? "is-" + kind : "");
    st.title = title || "";
  }

  function scheduleSave(row) {
    const key = row.dataset.iso;
    clearTimeout(saveTimers[key]);
    setStatus(row, "pending", "Modification en attente d'enregistrement");
    saveTimers[key] = setTimeout(() => save(row), SAVE_DELAY_MS);
  }

  async function save(row) {
    const key = row.dataset.iso;
    const fields = readRow(row);
    setStatus(row, "saving", "Enregistrement…");
    try {
      const d = await fetchJSON("/api/heures/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ employee: CFG.employee, date: key, fields }),
      });
      if (!d.ok) throw new Error(d.error || "Erreur.");
      const y = Number(key.slice(0, 4));
      (byYear[y] = byYear[y] || {})[key] = d.entry;
      setStatus(row, "ok", "Enregistré");
      renderSummaries();
    } catch (e) {
      setStatus(row, "error", e.message);
      showToast(`${key.split("-").reverse().join("/")} : ${e.message}`, "danger");
    }
  }

  function bindRow(row) {
    row.querySelectorAll("[data-f]").forEach((el) => {
      el.addEventListener("change", () => { refreshRow(row); scheduleSave(row); });
      if (el.type === "time") el.addEventListener("input", () => refreshRow(row));
    });
    row.querySelector(".hr-fill").addEventListener("click", () => {
      const d = parseIsoLocal(row.dataset.iso);
      const std = d.getDay() === 5 ? STANDARD_FRIDAY : STANDARD;
      for (const [f, v] of Object.entries(std)) row.querySelector(`[data-f="${f}"]`).value = v;
      refreshRow(row);
      scheduleSave(row);
    });
  }

  function shiftMonth(delta) {
    const d = new Date(year, month + delta, 1);
    year = d.getFullYear();
    month = d.getMonth();
    load();
  }

  document.getElementById("btn-prev").addEventListener("click", () => shiftMonth(-1));
  document.getElementById("btn-next").addEventListener("click", () => shiftMonth(1));
  document.getElementById("btn-today").addEventListener("click", () => {
    year = today.getFullYear();
    month = today.getMonth();
    load();
  });
  showWeekends.addEventListener("change", () => {
    try { localStorage.setItem("heures-weekends", showWeekends.checked ? "1" : "0"); } catch (e) { /* idem */ }
    render();
  });

  // Prévient la perte d'une saisie encore en attente (délai anti-rafale) à la fermeture.
  window.addEventListener("beforeunload", (ev) => {
    if (document.querySelector(".hr-status.is-pending, .hr-status.is-saving")) {
      ev.preventDefault();
      ev.returnValue = "";
    }
  });

  load();
})();
