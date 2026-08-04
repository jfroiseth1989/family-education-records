// Local, pre-ingestion drop-zone auto-fill preview (FERChronos document
// drop-zone auto-fill).
//
// On file select/drop, POSTs the file to this form's `data-preview-url`
// (see app/api/documents.py::preview_document) and, for any field the
// user hasn't already touched, fills in the suggested value with a
// visible "Suggested from: ..." note. Nothing here is ever silently
// finalized: every filled field stays fully editable, and the user's own
// edit always wins -- once a field is touched, no later suggestion (even
// from re-selecting the same file) overwrites it again until a new file
// is chosen. The whole request runs against this application's own
// origin only; no other network call is made from this script.
(function () {
  "use strict";

  function setupPreview(form) {
    var previewUrl = form.dataset.previewUrl;
    var fileInput = form.querySelector('input[type="file"]');
    if (!previewUrl || !fileInput) {
      return;
    }
    var dropzone = form.querySelector(".dropzone");
    var csrfInput = form.querySelector('input[name="csrf_token"]');

    var typeSelect = form.querySelector('select[name="document_type_id"]');
    var typeSource = form.querySelector('input[name="document_type_source"]');
    var typeNote = form.querySelector('[data-suggestion-note="document_type"]');

    var dateInput = form.querySelector('input[name="document_date"]');
    var precisionSelect = form.querySelector('select[name="document_date_precision"]');
    var rangeEndInput = form.querySelector('input[name="document_date_range_end"]');
    var dateSource = form.querySelector('input[name="document_date_source"]');
    var dateNote = form.querySelector('[data-suggestion-note="document_date"]');

    var receivedInput = form.querySelector('input[name="date_received"]');
    var receivedSource = form.querySelector('input[name="date_received_source"]');
    var receivedNote = form.querySelector('[data-suggestion-note="date_received"]');

    var infoNote = form.querySelector('[data-suggestion-note="informational"]');

    var touched = {};
    var suggestedValues = {};

    function showNote(el, text) {
      if (!el) {
        return;
      }
      el.textContent = text;
      el.hidden = false;
    }

    function clearNote(el) {
      if (!el) {
        return;
      }
      el.textContent = "";
      el.hidden = true;
    }

    function updateSource(field, currentValue, sourceInput) {
      if (!sourceInput) {
        return;
      }
      var suggestedValue = suggestedValues[field];
      if (suggestedValue === undefined) {
        sourceInput.value = currentValue ? "manual" : "";
        return;
      }
      if (!currentValue) {
        sourceInput.value = "cleared";
      } else if (currentValue === suggestedValue) {
        sourceInput.value = "accepted";
      } else {
        sourceInput.value = "edited";
      }
    }

    function watchField(el, field, sourceInput, valueGetter) {
      if (!el) {
        return;
      }
      ["input", "change"].forEach(function (eventName) {
        el.addEventListener(eventName, function () {
          touched[field] = true;
          updateSource(field, valueGetter(), sourceInput);
        });
      });
    }

    watchField(typeSelect, "document_type_id", typeSource, function () {
      return typeSelect.value;
    });
    watchField(dateInput, "document_date", dateSource, function () {
      return dateInput.value;
    });
    watchField(precisionSelect, "document_date", dateSource, function () {
      return dateInput.value;
    });
    watchField(rangeEndInput, "document_date", dateSource, function () {
      return dateInput.value;
    });
    watchField(receivedInput, "date_received", receivedSource, function () {
      return receivedInput.value;
    });

    function resetForNewFile() {
      touched = {};
      suggestedValues = {};
      [typeSource, dateSource, receivedSource].forEach(function (el) {
        if (el) {
          el.value = "";
        }
      });
      [typeNote, dateNote, receivedNote, infoNote].forEach(clearNote);
    }

    function applyTypeSuggestion(suggestion) {
      if (!suggestion || !typeSelect) {
        return;
      }
      suggestedValues.document_type_id = String(suggestion.type_id);
      if (!touched.document_type_id) {
        typeSelect.value = String(suggestion.type_id);
        if (typeSource) {
          typeSource.value = "suggested";
        }
      }
      showNote(
        typeNote,
        "Suggested document type: " + suggestion.type_name + " — change if incorrect"
      );
    }

    function applyDateSuggestion(field, suggestion, valueInput, sourceInput, noteEl, label) {
      if (!suggestion || !valueInput) {
        return;
      }
      suggestedValues[field] = suggestion.value;
      if (!touched[field]) {
        valueInput.value = suggestion.value;
        if (field === "document_date") {
          if (precisionSelect) {
            precisionSelect.value = suggestion.precision === "range" ? "range" : "exact";
          }
          if (rangeEndInput) {
            rangeEndInput.value = suggestion.range_end || "";
          }
        }
        if (sourceInput) {
          sourceInput.value = "suggested";
        }
      }
      showNote(noteEl, label + ' — suggested from: "' + suggestion.matched_phrase + '"');
    }

    function applyInformationalNotes(notes) {
      if (!notes || notes.length === 0 || !infoNote) {
        return;
      }
      var lines = notes.map(function (note) {
        return note.label + ": " + note.value + ' ("' + note.matched_phrase + '")';
      });
      showNote(infoNote, "Also found in text — " + lines.join("; ") + " — not auto-filled into any field.");
    }

    function runPreview(file) {
      resetForNewFile();
      if (!file) {
        return;
      }
      var body = new FormData();
      body.append("file", file);
      var headers = {};
      if (csrfInput && csrfInput.value) {
        headers["X-CSRF-Token"] = csrfInput.value;
      }
      fetch(previewUrl, {
        method: "POST",
        body: body,
        headers: headers,
        credentials: "same-origin",
      })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (data) {
          if (!data) {
            return;
          }
          applyTypeSuggestion(data.document_type);
          applyDateSuggestion(
            "document_date",
            data.document_date,
            dateInput,
            dateSource,
            dateNote,
            "Document date"
          );
          applyDateSuggestion(
            "date_received",
            data.date_received,
            receivedInput,
            receivedSource,
            receivedNote,
            "Date received"
          );
          applyInformationalNotes(data.informational_dates);
        })
        .catch(function () {
          // Best-effort local convenience only -- a failed/blocked preview
          // request never alters or blocks the actual upload below.
        });
    }

    fileInput.addEventListener("change", function () {
      runPreview(fileInput.files && fileInput.files[0]);
    });

    if (dropzone) {
      dropzone.addEventListener("drop", function (event) {
        var files = event.dataTransfer && event.dataTransfer.files;
        if (files && files.length > 0) {
          runPreview(files[0]);
        }
      });
    }
  }

  document.querySelectorAll("form[data-preview-url]").forEach(setupPreview);
})();
