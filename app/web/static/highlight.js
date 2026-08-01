// Text-offset highlight capture for the document viewer.
//
// Deliberately simple: the page text lives in a single <pre> element with
// exactly one text node (see document_viewer.html), so a selection's start
// and end offsets within that text node are the same offsets the server
// uses to slice `document_pages.extracted_text` when creating a highlight
// (see app/core/annotations/service.py create_highlight). No cross-node
// selections are supported -- if the browser reports a selection spanning
// more than one node, it is rejected rather than guessed at.
(function () {
  "use strict";

  const pageText = document.getElementById("page-text");
  const form = document.getElementById("highlight-form");
  if (!pageText || !form) {
    return;
  }

  const startInput = document.getElementById("highlight-start");
  const endInput = document.getElementById("highlight-end");
  const preview = document.getElementById("highlight-preview");
  const cancelButton = document.getElementById("highlight-cancel");

  function hideForm() {
    form.style.display = "none";
    startInput.value = "";
    endInput.value = "";
    preview.textContent = "";
  }

  pageText.addEventListener("mouseup", function () {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
      return;
    }

    const range = selection.getRangeAt(0);
    const textNode = pageText.firstChild;

    if (range.startContainer !== textNode || range.endContainer !== textNode) {
      // Selection isn't confined to the single text node we render -- bail
      // out rather than risk offsets that don't match the server's text.
      return;
    }

    const start = Math.min(range.startOffset, range.endOffset);
    const end = Math.max(range.startOffset, range.endOffset);
    if (start === end) {
      return;
    }

    startInput.value = String(start);
    endInput.value = String(end);
    preview.textContent = textNode.textContent.slice(start, end);
    form.style.display = "block";
  });

  if (cancelButton) {
    cancelButton.addEventListener("click", function () {
      window.getSelection().removeAllRanges();
      hideForm();
    });
  }
})();
