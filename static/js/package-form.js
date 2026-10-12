(function () {
  "use strict";

  // Anteprima della foto appena scattata, prima di salvare.
  var input = document.getElementById("id_photo");
  var preview = document.getElementById("photo-preview");
  var drop = document.querySelector(".photo-drop");
  var prompt = drop && drop.querySelector(".photo-prompt");

  if (input && preview && drop) {
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (!file) { return; }
      if (preview.dataset.objectUrl) { URL.revokeObjectURL(preview.dataset.objectUrl); }
      var url = URL.createObjectURL(file);
      preview.dataset.objectUrl = url;
      preview.src = url;
      preview.hidden = false;
      drop.classList.add("has-photo");
      if (prompt) { prompt.lastChild.textContent = "Cambia foto"; }
    });
  }

  // Con una connessione lenta il caricamento della foto richiede qualche secondo:
  // blocco i pulsanti dopo il primo tocco per non registrare lo stesso pacco due volte.
  var form = document.querySelector("form[data-submit-once]");
  if (!form) { return; }

  function setBusy(busy) {
    form.querySelectorAll("button[type=submit]").forEach(function (button) {
      button.disabled = busy;
    });
  }

  form.addEventListener("submit", function (event) {
    if (form.dataset.sent) { event.preventDefault(); return; }
    form.dataset.sent = "1";
    var submitter = event.submitter;
    if (submitter && submitter.name) {
      // I pulsanti disabilitati non vengono inviati: copio il valore in un campo nascosto.
      var hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = submitter.name;
      hidden.value = submitter.value;
      form.appendChild(hidden);
    }
    if (submitter) { submitter.textContent = "Salvataggio…"; }
    setBusy(true);
  });

  // Tornando indietro dalla cache del browser il modulo deve essere di nuovo utilizzabile.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) { delete form.dataset.sent; setBusy(false); }
  });
})();
