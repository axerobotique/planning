// Bascule mode clair / mode sombre, partagée par toutes les pages.
// Le thème initial est appliqué dans le <head> (script inline) avant le
// premier rendu pour éviter un flash blanc ; ce fichier ne gère que le bouton.
(function () {
  const KEY = "planning-theme";
  const root = document.documentElement;
  const btn = document.getElementById("btn-theme");
  if (!btn) return;

  function render() {
    const dark = root.dataset.theme === "dark";
    btn.innerHTML = dark ? "&#9728; Mode clair" : "&#9790; Mode sombre";
    btn.title = dark ? "Revenir au mode clair" : "Passer en mode sombre";
  }

  btn.addEventListener("click", () => {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem(KEY, next); } catch (e) { /* stockage bloqué : bascule non mémorisée */ }
    render();
  });

  render();
})();
