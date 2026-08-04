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
    var statusEl = form.querySelector("[data-preview-status]");

    var typeSelect = form.querySelector('select[name="document_type_id"]');
    var typeSource = form.querySelector('input[name="document_type_source"]');
    var typeNote = form.querySelector('[data-suggestion-note="document_type"]');

    var sourceInput = form.querySelector('input[name="source"]');
    var sourceSource = form.querySelector('input[name="source_source"]');
    var sourceNote = form.querySelector('[data-suggestion-note="source"]');

    var dateInput = form.querySelector('input[name="document_date"]');
    var precisionSelect = form.querySelector('select[name="document_date_precision"]');
    var rangeEndInput = form.querySelector('input[name="document_date_range_end"]');
    var dateSource = form.querySelector('input[name="document_date_source"]');
    var dateNote = form.querySelector('[data-suggestion-note="document_date"]');

    var receivedInput = form.querySelector('input[name="date_received"]');
    var receivedSource = form.querySelector('input[name="date_received_source"]');
    var receivedNote = form.querySelector('[data-suggestion-note="date_received"]');

    var notesInput = form.querySelector('textarea[name="notes"]');
    var notesSource = form.querySelector('input[name="notes_source"]');
    var notesNote = form.querySelector('[data-suggestion-note="notes"]');

    var infoNote = form.querySelector('[data-suggestion-note="informational"]');

    var touched = {};
    var suggestedValues = {};

    function setStatus(text) {
      if (!statusEl) {
        return;
      }
      statusEl.textContent = text;
      statusEl.hidden = !text;
    }

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
    watchField(sourceInput, "source", sourceSource, function () {
      return sourceInput.value;
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
    watchField(notesInput, "notes", notesSource, function () {
      return notesInput.value;
    });

    function resetForNewFile() {
      touched = {};
      suggestedValues = {};
      [typeSource, sourceSource, dateSource, receivedSource, notesSource].forEach(function (el) {
        if (el) {
          el.value = "";
        }
      });
      [typeNote, sourceNote, dateNote, receivedNote, notesNote, infoNote].forEach(clearNote);
    }

    function applyTypeSuggestion(suggestion) {
      if (!suggestion || !typeSelect) {
        return false;
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
      return true;
    }

    function applySourceSuggestion(suggestion) {
      if (!suggestion || !sourceInput) {
        return false;
      }
      suggestedValues.source = suggestion.value;
      if (!touched.source) {
        sourceInput.value = suggestion.value;
        if (sourceSource) {
          sourceSource.value = "suggested";
        }
      }
      showNote(sourceNote, 'Source — suggested from: "' + suggestion.matched_phrase + '"');
      return true;
    }

    function applyNotesSuggestion(suggestion) {
      if (!suggestion || !notesInput) {
        return false;
      }
      suggestedValues.notes = suggestion.value;
      if (!touched.notes) {
        notesInput.value = suggestion.value;
        if (notesSource) {
          notesSource.value = "suggested";
        }
      }
      showNote(
        notesNote,
        "Notes — composed locally from the extracted document type, dates, and source; edit or clear as needed."
      );
      return true;
    }

    function applyDateSuggestion(field, suggestion, valueInput, sourceInput, noteEl, label) {
      if (!suggestion || !valueInput) {
        return false;
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
      return true;
    }

    function applyInformationalNotes(notes) {
      if (!notes || notes.length === 0 || !infoNote) {
        return false;
      }
      var lines = notes.map(function (note) {
        return note.label + ": " + note.value + ' ("' + note.matched_phrase + '")';
      });
      showNote(infoNote, "Also found in text — " + lines.join("; ") + " — not auto-filled into any field.");
      return true;
    }

    function runPreview(file) {
      resetForNewFile();
      if (!file) {
        setStatus("");
        return;
      }
      setStatus("Analyzing document…");
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
            setStatus("Preview failed — you can still enter the fields manually");
            return;
          }
          var appliedAny = false;
          if (applyTypeSuggestion(data.document_type)) {
            appliedAny = true;
          }
          if (applySourceSuggestion(data.source)) {
            appliedAny = true;
          }
          if (
            applyDateSuggestion(
              "document_date",
              data.document_date,
              dateInput,
              dateSource,
              dateNote,
              "Document date"
            )
          ) {
            appliedAny = true;
          }
          if (
            applyDateSuggestion(
              "date_received",
              data.date_received,
              receivedInput,
              receivedSource,
              receivedNote,
              "Date received"
            )
          ) {
            appliedAny = true;
          }
          if (applyNotesSuggestion(data.notes)) {
            appliedAny = true;
          }
          var hasInformational = applyInformationalNotes(data.informational_dates);
          setStatus(appliedAny || hasInformational ? "Suggestions added" : "No reliable suggestions found");
        })
        .catch(function () {
          // Best-effort local convenience only -- a failed/blocked preview
          // request never alters or blocks the actual upload below.
          setStatus("Preview failed — you can still enter the fields manually");
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
