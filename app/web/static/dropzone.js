// Drag-and-drop enhancement for the document upload form (FERChronos UX
// refinement Step 3).
//
// Purely a front-end convenience layer: it only ever assigns files to the
// existing native <input type="file" name="file"> via a DataTransfer
// object, so the form still submits exactly the same multipart POST it
// always has -- nothing about ingestion, hashing, or read-only storage
// changes. If JS is disabled, the native file input is still visible and
// fully usable on its own; the drop zone degrades to a plain file picker.
(function () {
  "use strict";

  const dropzone = document.getElementById("dropzone");
  const input = document.getElementById("dropzone-input");
  const filenameDisplay = document.getElementById("dropzone-filename");
  if (!dropzone || !input || !filenameDisplay) {
    return;
  }

  function showFilename() {
    if (input.files && input.files.length > 0) {
      filenameDisplay.textContent = "Selected: " + input.files[0].name;
      filenameDisplay.hidden = false;
      dropzone.classList.add("has-file");
    } else {
      filenameDisplay.hidden = true;
      dropzone.classList.remove("has-file");
    }
  }

  dropzone.addEventListener("click", function (event) {
    // The native input already sits on top of the dropzone and handles its
    // own click-to-browse -- avoid opening the picker twice.
    if (event.target !== input) {
      input.click();
    }
  });

  dropzone.addEventListener("keydown", function (event) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input.click();
    }
  });

  input.addEventListener("change", showFilename);

  ["dragenter", "dragover"].forEach(function (eventName) {
    dropzone.addEventListener(eventName, function (event) {
      event.preventDefault();
      event.stopPropagation();
      dropzone.classList.add("dragover");
    });
  });

  ["dragleave", "dragend"].forEach(function (eventName) {
    dropzone.addEventListener(eventName, function (event) {
      event.preventDefault();
      event.stopPropagation();
      dropzone.classList.remove("dragover");
    });
  });

  dropzone.addEventListener("drop", function (event) {
    event.preventDefault();
    event.stopPropagation();
    dropzone.classList.remove("dragover");

    const files = event.dataTransfer && event.dataTransfer.files;
    if (files && files.length > 0) {
      input.files = files;
      showFilename();
    }
  });
})();
