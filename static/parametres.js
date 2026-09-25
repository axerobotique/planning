(function () {
  "use strict";

  const tbody = document.getElementById("codes-tbody");
  const newColor = document.getElementById("new-color");
  const newCode = document.getElementById("new-code");
  const newLabel = document.getElementById("new-label");
  const addBtn = document.getElementById("add-btn");

  let legend = window.INITIAL_LEGEND || {};

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

  // Timeout explicite : sans ça, un appel réseau qui ne répond jamais (Sheets
  // API lente/indisponible) laisse l'UI bloquée sans aucun retour visible.
  async function fetchJSON(url, opts, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || 15000);
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

  function render() {
    const entries = Object.entries(legend).sort((a, b) => a[0].localeCompare(b[0]));
    if (!entries.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="params-empty">Aucun code défini.</td></tr>';
      return;
    }
    tbody.innerHTML = entries.map(([code, info]) => `
      <tr data-code="${esc(code)}">
        <td class="col-color"><input type="color" class="row-color" value="${info.color}" title="Changer la couleur de ${esc(code)}"></td>
        <td class="col-code">${esc(code)}</td>
        <td><input type="text" class="label-input" value="${esc(info.label)}" title="Renommer le libellé"></td>
        <td class="col-actions"><button type="button" class="row-del" title="Supprimer ${esc(code)}">×</button></td>
      </tr>`).join("");

    tbody.querySelectorAll(".row-color").forEach((inp) => {
      inp.addEventListener("change", () => {
        const code = inp.closest("tr").dataset.code;
        saveCode(code, legend[code].label, inp.value, `Couleur de ${code} mise à jour.`);
      });
    });
    tbody.querySelectorAll(".label-input").forEach((inp) => {
      const commit = () => {
        const code = inp.closest("tr").dataset.code;
        const trimmed = inp.value.trim();
        if (!trimmed || trimmed === legend[code].label) { inp.value = legend[code].label; return; }
        saveCode(code, trimmed, legend[code].color, `Libellé de ${code} mis à jour.`);
      };
      inp.addEventListener("change", commit);
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); inp.blur(); } });
    });
    tbody.querySelectorAll(".row-del").forEach((btn) => {
      btn.addEventListener("click", () => {
        const code = btn.closest("tr").dataset.code;
        if (!confirm(`Supprimer le code ${code} ? Les tâches déjà écrites avec ce code perdront sa couleur/libellé.`)) return;
        deleteCode(code);
      });
    });
  }

  async function saveCode(code, label, color, successMsg) {
    try {
      const d = await fetchJSON("/api/codes/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, label, color }),
      });
      if (!d.ok) throw new Error(d.error || "Erreur.");
      legend = d.legend;
      render();
      showToast(successMsg);
    } catch (e) {
      showToast(e.message || "Erreur.", "danger");
      render();
    }
  }

  async function deleteCode(code) {
    try {
      const d = await fetchJSON("/api/codes/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      if (!d.ok) throw new Error(d.error || "Erreur.");
      legend = d.legend;
      render();
      showToast(`Code ${code} supprimé.`);
    } catch (e) {
      showToast(e.message || "Erreur.", "danger");
    }
  }

  async function addCode() {
    const code = newCode.value.trim().toUpperCase();
    const label = newLabel.value.trim();
    const color = newColor.value;
    if (!code || !label) {
      showToast("Renseigne un code et un libellé.", "danger");
      return;
    }
    addBtn.disabled = true;
    try {
      await saveCode(code, label, color, `Code ${code} ajouté.`);
      newCode.value = "";
      newLabel.value = "";
      newCode.focus();
    } finally {
      addBtn.disabled = false;
    }
  }

  addBtn.addEventListener("click", addCode);
  newCode.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addCode(); } });
  newLabel.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addCode(); } });

  render();
})();
