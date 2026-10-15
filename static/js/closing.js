(function () {
  "use strict";

  // Il prezzo si precompila e si aggiorna da solo mentre si scrive il peso, ma si può scrivere a mano
  // (uno sconto, un prezzo concordato). Il prezzo da listino lo calcola comunque il server con le stesse
  // regole (pacco piccolo a prezzo fisso, altrimenti peso × prezzo al kg, premium incluso): qui è un aiuto.
  var entry = document.querySelector("form[data-closing-entry]");
  if (entry) {
    var weight = entry.querySelector("input[name=weight_kg]");
    var premium = entry.querySelector("input[name=premium]");
    var price = entry.querySelector("input[name=price]");
    var hint = entry.querySelector("[data-price-hint]");
    var reset = entry.querySelector("[data-price-reset]");
    var resetLabel = entry.querySelector("[data-list-price]");
    var defaultHint = hint.textContent;
    var rules = {
      smallMaxG: parseInt(entry.dataset.smallMaxG, 10),
      smallCents: parseInt(entry.dataset.smallCents, 10),
      rateCents: parseInt(entry.dataset.rateCents, 10),
      premiumCents: parseInt(entry.dataset.premiumCents, 10)
    };
    var manual = false; // vero dopo che il prezzo è stato scritto a mano

    var toCents = function (text) {
      var number = parseFloat(String(text).replace(",", "."));
      return isFinite(number) && number >= 0 ? Math.round(number * 100) : null;
    };
    var format = function (cents) { return (cents / 100).toFixed(2).replace(".", ","); };

    var listCents = function () {
      var kg = parseFloat(String(weight.value).replace(",", "."));
      if (!isFinite(kg) || kg <= 0) { return null; }
      var grams = Math.round(kg * 1000);
      if (grams <= rules.smallMaxG) { return rules.smallCents; }
      return Math.round(grams * (premium.checked ? rules.premiumCents : rules.rateCents) / 1000);
    };

    // Mostra com'è il prezzo rispetto al listino (senza toccare quello che sta scrivendo l'utente).
    var showHint = function () {
      var list = listCents();
      var typed = toCents(price.value);
      var changed = manual && list !== null && typed !== null && typed !== list;
      reset.hidden = !changed;
      if (changed) {
        var diff = list - typed;
        resetLabel.textContent = format(list) + " €";
        hint.textContent = diff > 0
          ? "Listino " + format(list) + " €, sconto " + format(diff) + " €."
          : "Listino " + format(list) + " €, prezzo maggiorato di " + format(-diff) + " €.";
      } else {
        hint.textContent = defaultHint;
      }
    };

    // Peso o premium cambiano: finché il prezzo non è stato scritto a mano segue il listino.
    var update = function () {
      var list = listCents();
      if (!manual) { price.value = list === null ? "" : format(list); }
      showHint();
    };

    weight.addEventListener("input", update);
    premium.addEventListener("change", update);
    price.addEventListener("input", function () {
      // Riscrivere il prezzo calcolato, o svuotare il campo, rimette il prezzo in automatico.
      manual = price.value.trim() !== "" && toCents(price.value) !== listCents();
      showHint();
    });
    reset.addEventListener("click", function () {
      manual = false;
      update();
      price.focus();
    });

    // Se la pagina torna con dei valori (errore nel modulo), un prezzo diverso dal listino resta com'è.
    // Senza un peso valido il prezzo precompilato non ha senso: si ricalcola quando il peso arriva.
    var initialList = listCents();
    manual = initialList !== null && price.value.trim() !== "" && toCents(price.value) !== initialList;
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
