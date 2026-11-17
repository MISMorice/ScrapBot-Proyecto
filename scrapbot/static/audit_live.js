/* Auditoría en vivo.
 * En la primera página de Auditoría, consulta cada pocos segundos (data-poll-ms)
 * los registros nuevos que cumplen los filtros actuales y los agrega arriba,
 * sin recargar la página. */
(function () {
  "use strict";
  var body = document.querySelector("tbody[data-audit-live]");
  if (!body || !window.fetch) return;
  var url = body.getAttribute("data-audit-live");
  var lastId = parseInt(body.getAttribute("data-last-id") || "0", 10);
  var pollMs = parseInt(body.getAttribute("data-poll-ms") || "5000", 10) || 5000;
  var sep = url.indexOf("?") === -1 ? "?" : "&";

  function cell(value, className) {
    var td = document.createElement("td");
    td.textContent = value;
    if (className) td.className = className;
    return td;
  }

  function row(entry) {
    var tr = document.createElement("tr");
    tr.className = "live-new";
    tr.appendChild(cell(entry.created));
    tr.appendChild(cell(entry.username));
    tr.appendChild(cell(entry.action, "mono"));
    tr.appendChild(cell(entry.entity));
    tr.appendChild(cell(entry.detail));
    return tr;
  }

  function poll() {
    fetch(url + sep + "after=" + lastId, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        if (data.new && data.new.length) {
          var empty = body.querySelector("tr[data-empty]");
          if (empty) empty.remove();
          // data.new viene del más nuevo al más antiguo: se insertan en orden inverso arriba.
          data.new.slice().reverse().forEach(function (entry) { body.insertBefore(row(entry), body.firstChild); });
        }
        lastId = Math.max(lastId, data.last_id || 0);
      })
      .catch(function () { /* sin conexión: se reintenta en el próximo ciclo */ });
  }
  setInterval(poll, pollMs);
})();
