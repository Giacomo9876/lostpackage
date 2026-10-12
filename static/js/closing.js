(function () {
  "use strict";

  // Anteprima del prezzo mentre si scrive il peso. È solo un aiuto: il prezzo vero lo calcola il server
  // con le stesse regole (pacco piccolo a prezzo fisso, altrimenti peso × prezzo al kg, premium incluso).
  var entry = document.querySelector("form[data-closing-entry]");
  if (entry) {
    var weight = entry.querySelector("input[name=weight_kg]");
    var premium = entry.querySelector("input[name=premium]");
    var preview = entry.querySelector("[data-price-preview]");
    var rules = {
      smallMaxG: parseInt(entry.dataset.smallMaxG, 10),
      smallCents: parseInt(entry.dataset.smallCents, 10),
      rateCents: parseInt(entry.dataset.rateCents, 10),
      premiumCents: parseInt(entry.dataset.premiumCents, 10)
    };

    var update = function () {
      var kg = parseFloat(String(weight.value).replace(",", "."));
      if (!isFinite(kg) || kg <= 0) { preview.textContent = "—"; return; }
      var grams = Math.round(kg * 1000);
      var cents;
      if (grams <= rules.smallMaxG) {
        cents = rules.smallCents;
      } else {
        cents = Math.round(grams * (premium.checked ? rules.premiumCents : rules.rateCents) / 1000);
      }
      preview.textContent = (cents / 100).toFixed(2).replace(".", ",") + " €";
    };

    weight.addEventListener("input", update);
    premium.addEventListener("change", update);
    update();
    if (!weight.value) { weight.focus(); }
  }

  // Eliminare un pacco chiede conferma (un tocco sbagliato su un telefono è facile).
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) { event.preventDefault(); }
    });
  });

  // Con una connessione lenta il tocco sul pulsante può sembrare ignorato: si blocca dopo il primo
  // invio per non inserire due volte lo stesso pacco o lo stesso totale.
  document.querySelectorAll("form[data-submit-once]").forEach(function (form) {
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
  });
})();
