"use strict";
document.addEventListener("click", async event => {
  const button = event.target.closest("button[data-copy-reconstruction]");
  if (!button) return;
  try {
    await navigator.clipboard.writeText(button.dataset.copyReconstruction);
    button.textContent = "Copied selection details";
  } catch {
    button.textContent = "Copy unavailable — select the details above";
  }
});
