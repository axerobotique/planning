(function () {
  "use strict";

  const gridRoot = document.getElementById("grid-root");
  const bannerRoot = document.getElementById("banner-root");
  const legendRoot = document.getElementById("legend-root");
  const overlay = document.getElementById("modal-overlay");
  const form = document.getElementById("modal-form");
  const modalTitle = document.getElementById("modal-title");
  const modalError = document.getElementById("modal-error");
  const fEmployees = document.getElementById("f-employees");
  const fCode = document.getElementById("f-code");
  const fClient = document.getElementById("f-client");
  const fText = document.getElementById("f-text");
  const fDateStart = document.getElementById("f-date-start");
  const fDateEnd = document.getElementById("f-date-end");
  const btnDelete = document.getElementById("btn-delete");
  const btnDuplicate = document.getElementById("btn-duplicate");
  const btnCancel = document.getElementById("btn-cancel");
  const fAffaire = document.getElementById("f-affaire");
  const affaireTasksSection = document.getElementById("affaire-tasks");
  const affaireTasksList = document.getElementById("affaire-tasks-list");
  const affaireTaskNewInput = document.getElementById("affaire-task-new");
  const affaireTaskNewAssignee = document.getElementById("affaire-task-new-assignee");
  const affaireTaskAddBtn = document.getElementById("affaire-task-add-btn");
  const legendNewCode = document.getElementById("legend-new-code");
  const legendNewLabel = document.getElementById("legend-new-label");
  const legendNewColor = document.getElementById("legend-new-color");
  const legendAddBtn = document.getElementById("legend-add-btn");

  let weekOffset = window.PLANNING_WEEK_OFFSET || 0;
  let currentData = null;
  let modalOpen = false;
  let dragging = false;

  // État de l'édition en cours dans la modale : null si "nouvelle tâche".
  // `groupId`/`members` décrivent les autres techniciens déjà affectés à la
  // même tâche (cf. marqueur [GRP:] côté serveur) : édition/déplacement/
  // suppression leur sont propagés pour rester synchronisés.
  let editing = null; // { row, oldDates: [iso...], groupId, members: [{employee, row, old_dates}] }

  // Même limite que le serveur (cf. MAX_TASK_SPAN_DAYS dans app.py) : filet de
  // sécurité client pour échouer vite et clairement plutôt que d'envoyer une
  // plage de dates aberrante (cf. incident : un calcul de durée buggé avait
  // fini par écrire la même tâche sur ~400 jours dans le sheet).
  const MAX_TASK_SPAN_DAYS = 180;

  // Construit la date en UTC pur (pas de new Date(iso) sans "Z") : sinon,
  // dans un fuseau en avance sur UTC (France), minuit local franchit la
  // frontière du jour UTC et toISOString() renvoie la veille — décalage
  // d'un jour silencieux sur tous les calculs de date.
  function isoToUTCDate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  }

  function isoAddDays(iso, n) {
    const d = isoToUTCDate(iso);
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }

  // Différence en jours calculée directement (pas de boucle jour par jour) :
  // correcte quel que soit l'écart, sans limite arbitraire à contourner.
  function daysBetween(startIso, endIso) {
    const ms = isoToUTCDate(endIso) - isoToUTCDate(startIso);
    return Math.round(ms / 86400000) + 1;
  }

  function isoRange(startIso, endIso) {
    const n = daysBetween(startIso, endIso);
    if (n <= 0 || n > MAX_TASK_SPAN_DAYS) {
      throw new Error(`Plage de dates invalide (${n} jour(s)).`);
    }
    const out = [];
    for (let i = 0; i < n; i++) out.push(isoAddDays(startIso, i));
    return out;
  }

  // Recompose la même convention "CODE - Client\nTexte libre" que le serveur
  // sait parser (cf. parse_task_parts dans app.py) — le texte brut de la
  // cellule reste une seule chaîne, seule l'édition est structurée en 3
  // champs.
  function composeTaskText(code, client, texte) {
    code = (code || "").trim().toUpperCase();
    client = (client || "").trim();
    texte = (texte || "").trim();
    const firstLine = code ? (client ? `${code} - ${client}` : code) : client;
    return [firstLine, texte].filter(Boolean).join("\n");
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function showBanner(kind, text) {
    if (!text) { bannerRoot.innerHTML = ""; return; }
    bannerRoot.innerHTML = `<div class="banner banner-${esc(kind)}">${esc(text)}</div>`;
  }

  function showToast(text, kind) {
    const el = document.createElement("div");
    el.className = "toast toast-" + (kind || "success");
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  // Fetch avec timeout explicite : sans ça, un appel réseau qui ne répond
  // jamais (Sheets API lente/indisponible) laisse l'UI bloquée indéfiniment
  // sur "Chargement…" sans aucun retour à l'utilisateur.
  async function fetchJSON(url, opts, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || 15000);
    try {
      const resp = await fetch(url, { ...(opts || {}), signal: controller.signal });
      return await resp.json();
    } catch (e) {
      if (e.name === "AbortError") {
        throw new Error("Délai dépassé — le serveur ne répond pas.");
      }
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  async function loadGrid() {
    const resp = await fetch(`/api/grid?s=${encodeURIComponent(weekOffset)}`);
    const data = await resp.json();
    currentData = data;
    renderLegend(data.legend);
    renderGrid(data);
  }

  // La légende est re-rendue à chaque chargement (pas seulement au premier
  // rendu Jinja) pour refléter tout de suite un code ajouté/édité/supprimé
  // par un autre utilisateur — les codes sont gérés dans l'onglet "Codes" du
  // sheet, plus de dict en dur (cf. `read_codes` côté serveur).
  function renderLegend(legend) {
    if (!legend) return;
    legendRoot.innerHTML = Object.entries(legend).map(([code, info]) => `
      <span class="legend-item" data-code="${esc(code)}">
        <input type="color" class="swatch" value="${info.color}" data-code="${esc(code)}" title="Changer la couleur de ${esc(code)}">
        <span class="legend-label">${esc(code)} — ${esc(info.label)}</span>
        <button type="button" class="legend-edit" data-code="${esc(code)}" title="Renommer le libellé">✎</button>
        <button type="button" class="legend-del" data-code="${esc(code)}" title="Supprimer le code">×</button>
      </span>`).join("");

    legendRoot.querySelectorAll("input.swatch").forEach((inp) => {
      inp.addEventListener("change", async () => {
        const code = inp.dataset.code;
        const info = legend[code];
        await saveCode(code, info.label, inp.value, `Couleur de ${code} mise à jour.`);
      });
    });
    legendRoot.querySelectorAll(".legend-edit").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const code = btn.dataset.code;
        const info = legend[code];
        const label = prompt(`Libellé pour ${code} :`, info.label);
        if (label == null) return;
        const trimmed = label.trim();
        if (!trimmed || trimmed === info.label) return;
        await saveCode(code, trimmed, info.color, `Libellé de ${code} mis à jour.`);
      });
    });
    legendRoot.querySelectorAll(".legend-del").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const code = btn.dataset.code;
        if (!confirm(`Supprimer le code ${code} ? Les tâches déjà écrites avec ce code perdront sa couleur/libellé.`)) return;
        try {
          const d = await fetchJSON("/api/codes/delete", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code }),
          });
          if (!d.ok) throw new Error(d.error || "Erreur.");
          showToast(`Code ${code} supprimé.`);
          await loadGrid();
        } catch (e) {
          showToast(e.message || "Erreur.", "danger");
        }
      });
    });

    fillCodeOptions(legend, fCode.value);
  }

  async function saveCode(code, label, color, successMsg) {
    try {
      const d = await fetchJSON("/api/codes/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, label, color }),
      });
      if (!d.ok) throw new Error(d.error || "Erreur.");
      showToast(successMsg);
      await loadGrid();
    } catch (e) {
      showToast(e.message || "Erreur.", "danger");
    }
  }

  async function addCode() {
    const code = legendNewCode.value.trim().toUpperCase();
    const label = legendNewLabel.value.trim();
    const color = legendNewColor.value;
    if (!code || !label) {
      showToast("Renseigne un code et un libellé.", "danger");
      return;
    }
    legendAddBtn.disabled = true;
    try {
      await saveCode(code, label, color, `Code ${code} ajouté.`);
      legendNewCode.value = "";
      legendNewLabel.value = "";
    } finally {
      legendAddBtn.disabled = false;
    }
  }

  legendAddBtn.addEventListener("click", addCode);
  legendNewLabel.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addCode(); }
  });

  // Options du <select> Code de la modale, tenues à jour à chaque rendu de
  // légende (plutôt que figées au chargement Jinja) : sans ça, un code
  // ajouté/supprimé après coup n'apparaissait pas/restait sélectionnable
  // dans la modale tant que la page n'était pas rechargée.
  function fillCodeOptions(legend, selected) {
    fCode.innerHTML = '<option value="">—</option>' + Object.entries(legend).map(([code, info]) =>
      `<option value="${esc(code)}" ${code === selected ? "selected" : ""}>${esc(code)} — ${esc(info.label)}</option>`
    ).join("");
  }

  function renderGrid(data) {
    if (data.error) {
      showBanner("error", data.error);
      gridRoot.innerHTML = "";
      return;
    }
    showBanner(data.warning ? "warn" : null, data.warning);

    if (!data.employees.length) {
      gridRoot.innerHTML = '<p class="empty">Aucune donnée trouvée pour cette période.</p>';
      return;
    }

    // Regroupe les jours consécutifs de même semaine ISO sous un seul <th
    // colspan> : la fenêtre affichée commence toujours un lundi (cf.
    // `build_grid`) mais peut se terminer avant un dimanche (fin de la plage
    // couverte par le sheet), d'où un regroupement par valeur plutôt qu'un
    // colspan fixe à 7.
    const weekGroups = [];
    data.days.forEach((d) => {
      const last = weekGroups[weekGroups.length - 1];
      if (last && last.week === d.week) last.span += 1;
      else weekGroups.push({ week: d.week, span: 1 });
    });
    const weekRow = weekGroups.map((g) =>
      `<th class="col-week" colspan="${g.span}">Semaine ${g.week}</th>`
    ).join("");

    const thead = data.days.map((d) => {
      const cls = ["col-day"];
      if (d.is_today) cls.push("is-today");
      if (d.is_weekend) cls.push("is-weekend");
      if (d.new_week) cls.push("week-start");
      return `<th class="${cls.join(" ")}"><div class="dow">${esc(d.dow)}</div><div class="date">${esc(d.label)}</div></th>`;
    }).join("");

    const rows = data.employees.map((emp, empIdx) => {
      const slotCount = emp.rows || 1;
      const altCls = empIdx % 2 === 0 ? "emp-a" : "emp-b";
      let trs = "";
      for (let s = 0; s < slotCount; s++) {
        const cells = data.days.map((d, j) => {
          const cls = ["cell"];
          if (d.is_today) cls.push("is-today");
          if (d.is_weekend) cls.push("is-weekend");
          if (d.new_week) cls.push("week-start");
          // Une seule tâche par cellule-jour ici : chaque créneau (slot) est
          // sa propre ligne <tr>, ce qui garantit (via le rendu natif des
          // lignes de table) que toutes les cellules d'un même créneau
          // partagent la même hauteur d'un jour à l'autre — sans ça, un
          // texte qui passe à la ligne dans une colonne décale les créneaux
          // suivants uniquement dans cette colonne (cf. bug d'alignement).
          const frag = (emp.cells[j] || []).find((t) => t.slot === s);
          const html = frag ? renderTask(frag, emp.name) : "";
          return `<td class="${cls.join(" ")}" data-date="${d.iso}" data-employee="${esc(emp.name)}">${html}</td>`;
        }).join("");
        const nameCell = s === 0
          ? `<th class="col-name" rowspan="${slotCount}">
              <div class="emp-name-wrap">
                <span class="emp-name">${esc(emp.name)}</span>
                <span class="emp-row-controls">
                  <button type="button" class="emp-row-btn emp-row-remove" data-employee="${esc(emp.name)}" title="Supprimer une ligne">−</button>
                  <button type="button" class="emp-row-btn emp-row-add" data-employee="${esc(emp.name)}" title="Ajouter une ligne">+</button>
                </span>
              </div>
            </th>`
          : "";
        trs += `<tr class="${altCls}" data-employee="${esc(emp.name)}">${nameCell}${cells}</tr>`;
      }
      return trs;
    }).join("");

    gridRoot.innerHTML = `<table class="planning">
      <thead>
        <tr class="row-week"><th class="col-name"></th>${weekRow}</tr>
        <tr class="row-date"><th class="col-name"></th>${thead}</tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;

    bindGridEvents();
  }

  function renderTask(t, employeeName) {
    if (t.placeholder) {
      return '<div class="task placeholder"></div>';
    }
    const cls = ["task"];
    if (!t.is_start) cls.push("seg-cont-left");
    if (!t.is_end) cls.push("seg-cont-right");
    if (t.truncated) cls.push("truncated");
    const style = `background:${t.bg}; color:${t.fg};${t.italic ? " font-style:italic;" : ""}`;
    // Tâche tronquée (continue hors de la fenêtre affichée, ex: astreinte sans
    // date de fin) : pas de drag, sa vraie étendue n'est pas connue ici — la
    // déplacer écrirait la mauvaise plage dans le sheet (cf. bug doublon).
    const draggable = t.truncated ? "false" : "true";
    const title = t.truncated
      ? ' title="Cette tâche continue au-delà de la période affichée — déplacement désactivé, utilisez la fiche (clic) pour la modifier."'
      : "";
    // Le contenu n'est affiché que sur la 1ère cellule du bloc multi-jours :
    // les jours suivants n'affichent que la couleur continue, pas le texte
    // répété.
    let body = "";
    if (t.is_start) {
      const headText = t.code
        ? `<span class="task-code">${esc(t.code)}</span>${t.client ? " " + esc(t.client) : ""}`
        : esc(t.client);
      const head = (t.code || t.client) ? `<div class="task-head">${headText}</div>` : "";
      const affaireTag = t.affaire ? `<span class="task-affaire">#${esc(t.affaire)}</span>` : "";
      // Texte libre en petite police, tronqué à quelques lignes (cf. .task-texte
      // dans le CSS) avec un bouton pour déployer : sans ça, une description
      // longue fait grossir toute la ligne de la grille.
      const texteBlock = t.texte
        ? `<div class="task-texte">${esc(t.texte)}</div><button type="button" class="task-expand">▾ voir plus</button>`
        : "";
      body = head + affaireTag + texteBlock;
    }
    return `<div class="${cls.join(" ")}" draggable="${draggable}" style="${style}"${title}
        data-row="${t.row}" data-date-start="${t.date_start}" data-date-end="${t.date_end}"
        data-employee="${esc(employeeName)}" data-text="${esc(t.raw_text)}" data-code="${esc(t.code)}"
        data-client="${esc(t.client)}" data-texte="${esc(t.texte)}"
        data-affaire="${esc(t.affaire || "")}" data-group="${esc(t.group || "")}">${body}</div>`;
  }

  function bindGridEvents() {
    gridRoot.querySelectorAll(".emp-row-add").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const employee = btn.dataset.employee;
        btn.disabled = true;
        try {
          const d = await fetchJSON("/api/employee/row/add", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ employee }),
          });
          if (!d.ok) throw new Error(d.error || "Erreur.");
          await loadGrid();
        } catch (err) {
          showToast(err.message || "Erreur.", "danger");
          btn.disabled = false;
        }
      });
    });

    gridRoot.querySelectorAll(".emp-row-remove").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const employee = btn.dataset.employee;
        btn.disabled = true;
        try {
          const d = await fetchJSON("/api/employee/row/remove", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ employee }),
          });
          if (!d.ok) throw new Error(d.error || "Erreur.");
          await loadGrid();
        } catch (err) {
          showToast(err.message || "Erreur.", "danger");
          btn.disabled = false;
        }
      });
    });

    gridRoot.querySelectorAll(".task-expand").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const taskEl = btn.closest(".task");
        const expanded = taskEl.classList.toggle("expanded");
        btn.textContent = expanded ? "▴ réduire" : "▾ voir plus";
      });
    });

    gridRoot.querySelectorAll(".task:not(.placeholder)").forEach((el) => {
      el.addEventListener("click", (e) => {
        if (e.target.closest(".task-expand")) return;
        e.stopPropagation();
        openEditModal({
          row: Number(el.dataset.row),
          employee: el.dataset.employee,
          text: el.dataset.text,
          code: el.dataset.code,
          client: el.dataset.client,
          texte: el.dataset.texte,
          affaire: el.dataset.affaire,
          group: el.dataset.group,
          dateStart: el.dataset.dateStart,
          dateEnd: el.dataset.dateEnd,
        });
      });
      el.addEventListener("dragstart", (e) => {
        dragging = true;
        el.classList.add("dragging");
        e.dataTransfer.effectAllowed = "copyMove";
        e.dataTransfer.setData("application/json", JSON.stringify({
          row: Number(el.dataset.row),
          employee: el.dataset.employee,
          text: el.dataset.text,
          affaire: el.dataset.affaire,
          dateStart: el.dataset.dateStart,
          dateEnd: el.dataset.dateEnd,
        }));
      });
      el.addEventListener("dragend", () => {
        dragging = false;
        el.classList.remove("dragging");
      });
    });

    gridRoot.querySelectorAll("td.cell").forEach((td) => {
      td.addEventListener("click", () => {
        openCreateModal(td.dataset.employee, td.dataset.date);
      });
      td.addEventListener("dragover", (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = e.ctrlKey ? "copy" : "move";
        td.classList.add("drag-over");
      });
      td.addEventListener("dragleave", () => td.classList.remove("drag-over"));
      td.addEventListener("drop", async (e) => {
        e.preventDefault();
        td.classList.remove("drag-over");
        let payload;
        try {
          payload = JSON.parse(e.dataTransfer.getData("application/json"));
        } catch (err) {
          return;
        }
        const mode = e.ctrlKey ? "duplicate" : "move";
        let sourceDates, span, newStart, newEnd;
        try {
          sourceDates = isoRange(payload.dateStart, payload.dateEnd);
          span = sourceDates.length;
          newStart = td.dataset.date;
          newEnd = isoAddDays(newStart, span - 1);
        } catch (err) {
          showToast(err.message || "Plage de dates invalide.", "danger");
          return;
        }

        const resp = await fetch("/api/task/relocate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            row: payload.row,
            dates: sourceDates,
            text: payload.text,
            affaire: payload.affaire,
            target_employee: td.dataset.employee,
            date_start: newStart,
            date_end: newEnd,
            mode,
          }),
        });
        const d = await resp.json();
        if (!d.ok) { showToast(d.error || "Erreur.", "danger"); return; }
        showToast(mode === "move" ? "Tâche déplacée." : "Tâche dupliquée.");
        await loadGrid();
      });
    });
  }

  function fillEmployeeCheckboxes(selected) {
    const names = (currentData && currentData.employees || []).map((e) => e.name);
    const selectedSet = new Set(selected || []);
    fEmployees.innerHTML = names.map((n) =>
      `<label><input type="checkbox" value="${esc(n)}" ${selectedSet.has(n) ? "checked" : ""}> ${esc(n)}</label>`
    ).join("");
  }

  function getSelectedEmployees() {
    return Array.from(fEmployees.querySelectorAll("input[type=checkbox]:checked")).map((c) => c.value);
  }

  function assigneeOptionsHtml(selected) {
    const names = (currentData && currentData.employees || []).map((e) => e.name);
    return `<option value="">—</option>` + names.map((n) =>
      `<option value="${esc(n)}" ${n === selected ? "selected" : ""}>${esc(n)}</option>`
    ).join("");
  }

  function openModal() {
    modalOpen = true;
    modalError.hidden = true;
    overlay.hidden = false;
  }

  function closeModal() {
    modalOpen = false;
    overlay.hidden = true;
    editing = null;
    clearTimeout(affaireTasksDebounce);
  }

  function openCreateModal(employee, dateIso) {
    editing = null;
    modalTitle.textContent = "Nouvelle tâche";
    fillEmployeeCheckboxes(employee ? [employee] : []);
    fCode.value = "";
    fClient.value = "";
    fText.value = "";
    fAffaire.value = "";
    fDateStart.value = dateIso;
    fDateEnd.value = dateIso;
    btnDelete.hidden = true;
    btnDuplicate.hidden = true;
    affaireTaskNewAssignee.innerHTML = assigneeOptionsHtml(employee);
    openModal();
    refreshAffaireTasksSection();
    fClient.focus();
  }

  function openEditModal(task) {
    let oldDates, members, selectedEmployees;
    try {
      oldDates = isoRange(task.dateStart, task.dateEnd);
      // Autres techniciens déjà affectés à cette même tâche (même groupe,
      // cf. `groups` renvoyé par /api/grid) : on pré-coche leurs cases et on
      // garde leur ligne/dates pour propager l'édition/suppression.
      const groupId = task.group || "";
      const groupMembers = (groupId && currentData && currentData.groups && currentData.groups[groupId]) || [];
      const others = groupMembers.filter((m) => !(m.row === task.row && m.employee === task.employee));
      members = others.map((m) => ({
        employee: m.employee,
        row: m.row,
        old_dates: isoRange(m.date_start, m.date_end),
      }));
      selectedEmployees = Array.from(new Set([task.employee, ...others.map((m) => m.employee)]));
    } catch (err) {
      showToast(err.message || "Plage de dates invalide.", "danger");
      return;
    }
    editing = { row: task.row, oldDates, groupId: task.group || "", members };
    modalTitle.textContent = "Éditer la tâche";
    fillEmployeeCheckboxes(selectedEmployees);
    fCode.value = task.code || "";
    fClient.value = task.client || "";
    fText.value = task.texte || "";
    fAffaire.value = task.affaire || "";
    fDateStart.value = task.dateStart;
    fDateEnd.value = task.dateEnd;
    btnDelete.hidden = false;
    btnDuplicate.hidden = false;
    affaireTaskNewAssignee.innerHTML = assigneeOptionsHtml(task.employee);
    openModal();
    refreshAffaireTasksSection();
    fClient.focus();
  }

  // --- Checklist de tâches liée au numéro d'affaire saisi dans la modale ---
  // Stockée dans un onglet séparé ("Taches"), partagée entre toutes les
  // tâches planning qui référencent le même numéro d'affaire.

  let affaireTasksDebounce = null;

  async function refreshAffaireTasksSection() {
    const numero = fAffaire.value.trim();
    if (!numero) {
      affaireTasksSection.hidden = true;
      affaireTasksList.innerHTML = "";
      return;
    }
    affaireTasksSection.hidden = false;
    affaireTasksList.innerHTML = '<li class="loading-item">Chargement…</li>';
    try {
      const d = await fetchJSON(`/api/affaire/tasks?numero=${encodeURIComponent(numero)}`);
      if (!d.ok) { affaireTasksList.innerHTML = `<li class="loading-item">${esc(d.error || "Erreur de chargement.")}</li>`; return; }
      renderAffaireTasks(d.tasks || []);
    } catch (e) {
      affaireTasksList.innerHTML = `<li class="loading-item">${esc(e.message || "Erreur de chargement.")}</li>`;
    }
  }

  function renderAffaireTasks(tasks) {
    if (!tasks.length) {
      affaireTasksList.innerHTML = '<li class="loading-item">Aucune tâche pour cette affaire.</li>';
      return;
    }
    affaireTasksList.innerHTML = tasks.map((t) => `
      <li data-row="${t.row}">
        <label>
          <input type="checkbox" class="affaire-task-check" ${t.fait ? "checked" : ""}>
          <span class="${t.fait ? "done" : ""}">${esc(t.texte)}</span>
        </label>
        <select class="affaire-task-assignee" title="Assigner à">${assigneeOptionsHtml(t.assigne || "")}</select>
        <button type="button" class="affaire-task-del" title="Supprimer">×</button>
      </li>`).join("");

    affaireTasksList.querySelectorAll(".affaire-task-check").forEach((chk) => {
      chk.addEventListener("change", async () => {
        const row = Number(chk.closest("li").dataset.row);
        const prevChecked = !chk.checked;
        chk.disabled = true;
        try {
          const d = await fetchJSON("/api/affaire/tasks/toggle", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ row, fait: chk.checked }),
          });
          if (!d.ok) throw new Error(d.error || "Erreur.");
          chk.nextElementSibling.classList.toggle("done", chk.checked);
        } catch (e) {
          chk.checked = prevChecked;
          showToast(e.message || "Erreur.", "danger");
        } finally {
          chk.disabled = false;
        }
      });
    });
    affaireTasksList.querySelectorAll(".affaire-task-assignee").forEach((sel) => {
      sel.addEventListener("change", async () => {
        const row = Number(sel.closest("li").dataset.row);
        sel.disabled = true;
        try {
          const d = await fetchJSON("/api/affaire/tasks/assign", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ row, assigne: sel.value }),
          });
          if (!d.ok) throw new Error(d.error || "Erreur.");
        } catch (e) {
          showToast(e.message || "Erreur.", "danger");
        } finally {
          sel.disabled = false;
        }
      });
    });
    affaireTasksList.querySelectorAll(".affaire-task-del").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const li = btn.closest("li");
        const row = Number(li.dataset.row);
        btn.disabled = true;
        try {
          const d = await fetchJSON("/api/affaire/tasks/delete", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ row }),
          });
          if (!d.ok) throw new Error(d.error || "Erreur.");
          li.remove();
          if (!affaireTasksList.children.length) {
            affaireTasksList.innerHTML = '<li class="loading-item">Aucune tâche pour cette affaire.</li>';
          }
        } catch (e) {
          showToast(e.message || "Erreur.", "danger");
          btn.disabled = false;
        }
      });
    });
  }

  async function addAffaireTask() {
    const numero = fAffaire.value.trim();
    const texte = affaireTaskNewInput.value.trim();
    if (!numero || !texte) return;
    affaireTaskAddBtn.disabled = true;
    try {
      const d = await fetchJSON("/api/affaire/tasks/add", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ numero_affaire: numero, texte, assigne: affaireTaskNewAssignee.value }),
      });
      if (!d.ok) { showToast(d.error || "Erreur.", "danger"); return; }
      affaireTaskNewInput.value = "";
      renderAffaireTasks(d.tasks || []);
    } catch (e) {
      showToast(e.message || "Erreur.", "danger");
    } finally {
      affaireTaskAddBtn.disabled = false;
    }
  }

  affaireTaskAddBtn.addEventListener("click", addAffaireTask);
  affaireTaskNewInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addAffaireTask(); }
  });
  fAffaire.addEventListener("input", () => {
    clearTimeout(affaireTasksDebounce);
    affaireTasksDebounce = setTimeout(refreshAffaireTasksSection, 400);
  });

  function showModalError(msg) {
    modalError.textContent = msg;
    modalError.hidden = false;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (fDateEnd.value < fDateStart.value) {
      showModalError("La date de fin doit être postérieure ou égale à la date de début.");
      return;
    }
    const text = composeTaskText(fCode.value, fClient.value, fText.value);
    if (!text) {
      showModalError("Renseigne au moins un code, un client ou un texte.");
      return;
    }
    const employees = getSelectedEmployees();
    if (!employees.length) {
      showModalError("Sélectionne au moins un technicien.");
      return;
    }
    const body = {
      row: editing ? editing.row : null,
      old_dates: editing ? editing.oldDates : [],
      group_id: editing ? editing.groupId : "",
      members: editing ? editing.members : [],
      employees,
      text,
      affaire: fAffaire.value.trim(),
      date_start: fDateStart.value,
      date_end: fDateEnd.value,
    };
    const resp = await fetch("/api/task/save", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const d = await resp.json();
    if (!d.ok) { showModalError(d.error || "Erreur."); return; }
    closeModal();
    showToast("Tâche enregistrée.");
    await loadGrid();
  });

  btnDelete.addEventListener("click", async () => {
    if (!editing) return;
    const groupSize = 1 + editing.members.length;
    const msg = groupSize > 1
      ? `Supprimer cette tâche pour les ${groupSize} techniciens concernés ?`
      : "Supprimer cette tâche ?";
    if (!confirm(msg)) return;
    const instances = [{ row: editing.row, dates: editing.oldDates }]
      .concat(editing.members.map((m) => ({ row: m.row, dates: m.old_dates })));
    const resp = await fetch("/api/task/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instances }),
    });
    const d = await resp.json();
    if (!d.ok) { showModalError(d.error || "Erreur."); return; }
    closeModal();
    showToast("Tâche supprimée.");
    await loadGrid();
  });

  btnDuplicate.addEventListener("click", async () => {
    if (!editing) return;
    if (fDateEnd.value < fDateStart.value) {
      showModalError("La date de fin doit être postérieure ou égale à la date de début.");
      return;
    }
    const text = composeTaskText(fCode.value, fClient.value, fText.value);
    if (!text) {
      showModalError("Renseigne au moins un code, un client ou un texte.");
      return;
    }
    // Dupliquer crée des copies indépendantes (pas de groupe synchronisé) sur
    // chaque technicien coché, contrairement à "Enregistrer" qui garde les
    // techniciens liés.
    const employees = getSelectedEmployees();
    if (!employees.length) {
      showModalError("Sélectionne au moins un technicien.");
      return;
    }
    for (const targetEmployee of employees) {
      const resp = await fetch("/api/task/relocate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          row: editing.row,
          dates: editing.oldDates,
          text,
          affaire: fAffaire.value.trim(),
          target_employee: targetEmployee,
          date_start: fDateStart.value,
          date_end: fDateEnd.value,
          mode: "duplicate",
        }),
      });
      const d = await resp.json();
      if (!d.ok) { showModalError(d.error || "Erreur."); return; }
    }
    closeModal();
    showToast("Tâche dupliquée.");
    await loadGrid();
  });

  btnCancel.addEventListener("click", closeModal);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modalOpen) closeModal(); });

  document.getElementById("btn-prev").addEventListener("click", () => { weekOffset -= 1; loadGrid(); });
  document.getElementById("btn-next").addEventListener("click", () => { weekOffset += 1; loadGrid(); });
  document.getElementById("btn-today").addEventListener("click", () => { weekOffset = 0; loadGrid(); });
  document.getElementById("btn-new").addEventListener("click", () => {
    const today = new Date().toISOString().slice(0, 10);
    const firstEmployee = (currentData && currentData.employees[0] && currentData.employees[0].name) || "";
    openCreateModal(firstEmployee, today);
  });

  loadGrid();
  setInterval(() => { if (!modalOpen && !dragging) loadGrid(); }, 300000);
})();
