/* Aviso en vivo de alertas de precio.
 * El bot captura en segundo plano; esta página consulta cada 20 s si hay alertas
 * nuevas y las muestra como aviso, sin recargar ni depender de que alguien mire. */
(function () {
  "use strict";
  var area = document.querySelector(".toast-area");
  if (!area || !window.fetch) return;
  var url = area.getAttribute("data-alerts-url");
  var page = area.getAttribute("data-alerts-page");
  var lastId = parseInt(area.getAttribute("data-last-alert") || "0", 10);
  var badge = document.querySelector("[data-alert-badge]");

  function text(tag, value) { var el = document.createElement(tag); el.textContent = value; return el; }

  function show(alert) {
    var box = document.createElement("div");
    box.className = "toast";
    box.setAttribute("role", "status");
    var close = text("button", "×");
    close.setAttribute("aria-label", "Cerrar aviso");
    close.addEventListener("click", function () { box.remove(); });
    box.appendChild(close);
    var kind = alert.kind === "acumulada" ? "Variación acumulada" : "Cambio brusco de precio";
    box.appendChild(text("b", kind + " · " + alert.variation_pct.toLocaleString("es-CL") + " %"));
    var link = text("a", alert.name);
    link.href = alert.url;
    box.appendChild(link);
    box.appendChild(text("div", alert.source_name + ": " + alert.reference + " → " + alert.price + " (ya publicado)"));
    area.appendChild(box);
    setTimeout(function () { box.remove(); }, 20000);
  }

  function poll() {
    fetch(url + "?after=" + lastId, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        if (badge) { badge.textContent = data.unseen; badge.hidden = !data.unseen; }
        if (data.new && data.new.length) {
          data.new.slice().reverse().forEach(show);
          if (data.new.length >= 5) {
            var more = document.createElement("div");
            more.className = "toast";
            var a = text("a", "Ver todas las alertas nuevas");
            a.href = page;
            more.appendChild(a);
            area.appendChild(more);
          }
        }
        lastId = Math.max(lastId, data.last_id || 0);
      })
      .catch(function () { /* sin conexión: se reintenta en el próximo ciclo */ });
  }
  setInterval(poll, 20000);
})();
