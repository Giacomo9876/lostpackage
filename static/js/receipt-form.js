(function () {
  "use strict";

  var form = document.querySelector("form[data-receipt-form]");
  if (!form) { return; }

  var input = document.getElementById("id_photo");
  var amount = document.getElementById("id_receipts_total");
  var token = document.getElementById("id_reading_token");
  var status = form.querySelector("[data-reading-status]");
  var preview = document.getElementById("photo-preview");
  var drop = form.querySelector(".photo-drop");
  var prompt = drop && drop.querySelector(".photo-prompt");
  var readUrl = form.dataset.readUrl;
  var csrf = form.querySelector("input[name=csrfmiddlewaretoken]");

  var typed = false; // vero se l'importo è stato scritto a mano: la lettura non lo sovrascrive
  var filled = null; // l'ultimo importo compilato dalla lettura
  var counter = 0; // una sola lettura conta: l'ultima richiesta

  function say(text, kind) {
    if (!status) { return; }
    status.textContent = text;
    status.className = "reading" + (kind ? " reading-" + kind : "");
    status.hidden = !text;
  }

  function showPreview(file) {
    if (!preview || !drop) { return; }
    if (preview.dataset.objectUrl) { URL.revokeObjectURL(preview.dataset.objectUrl); }
    var url = URL.createObjectURL(file);
    preview.dataset.objectUrl = url;
    preview.src = url;
    preview.hidden = false;
    drop.classList.add("has-photo");
    if (prompt) { prompt.lastChild.textContent = "Cambia foto"; }
  }

  if (amount) {
    amount.addEventListener("input", function () {
      typed = amount.value.trim() !== "" && amount.value.trim() !== filled;
    });
  }

  function read(file) {
    var id = ++counter;
    if (token) { token.value = ""; }
    say("Leggo lo scontrino…", "busy");
    var data = new FormData();
    data.append("photo", file);
    var controller = window.AbortController ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, 45000) : null;
    fetch(readUrl, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrf ? csrf.value : "", "X-Requested-With": "XMLHttpRequest" },
      signal: controller ? controller.signal : undefined
    }).then(function (response) {
      return response.json().then(function (body) { return { ok: response.ok, body: body }; });
    }).then(function (result) {
      if (id !== counter) { return; }
      var body = result.body;
      if (!result.ok) { say(body.error || "Non riesco a leggere la foto: scrivi l'importo a mano.", "warn"); return; }
      if (!body.available) { say("", ""); return; }
      if (token) { token.value = body.token || ""; }
      if (!body.amount) {
        say("Non riesco a leggere l'importo dalla foto: scrivilo a mano.", "warn");
        return;
      }
      var detail = body.line ? " (riga «" + body.line + "»)" : "";
      if (amount && !typed) {
        amount.value = body.amount;
        filled = body.amount;
        say("Letto dalla foto: " + body.amount + " €" + detail + ". Controlla che sia giusto, altrimenti correggilo.", "ok");
      } else {
        say("Dalla foto si legge " + body.amount + " €" + detail + ", ma hai scritto un altro importo: resta il tuo.", "warn");
      }
    }).catch(function () {
      if (id !== counter) { return; }
      say("Non riesco a leggere la foto adesso: scrivi l'importo a mano.", "warn");
    }).then(function () {
      if (timer) { clearTimeout(timer); }
    });
  }

  if (input) {
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (!file) { counter++; say("", ""); if (token) { token.value = ""; } return; }
      showPreview(file);
      if (readUrl) { read(file); }
    });
  }

  // Con una connessione lenta il caricamento della foto richiede qualche secondo: dopo il primo
  // tocco il pulsante si blocca, per non salvare due volte la stessa giornata.
  function setBusy(busy) {
    form.querySelectorAll("button[type=submit]").forEach(function (button) { button.disabled = busy; });
  }
  form.addEventListener("submit", function (event) {
    if (form.dataset.sent) { event.preventDefault(); return; }
    form.dataset.sent = "1";
    var submitter = event.submitter;
    if (submitter) { submitter.textContent = "Salvataggio…"; }
    setBusy(true);
  });
  // Tornando indietro dalla cache del browser il modulo deve essere di nuovo utilizzabile.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) { delete form.dataset.sent; setBusy(false); }
  });

  // Eliminare una chiusura chiede conferma (un tocco sbagliato su un telefono è facile).
  document.querySelectorAll("form[data-confirm]").forEach(function (other) {
    other.addEventListener("submit", function (event) {
      if (!window.confirm(other.dataset.confirm)) { event.preventDefault(); }
    });
  });
})();
