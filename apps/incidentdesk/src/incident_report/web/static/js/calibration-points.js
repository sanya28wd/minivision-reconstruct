"use strict";
const source = document.getElementById("calibration-source");
const status = document.getElementById("calibration-status");
const form = document.getElementById("calibration-form");
source.addEventListener("click", (event) => {
  const rect = source.getBoundingClientRect();
  const x = (event.clientX - rect.left) * source.naturalWidth / rect.width;
  const y = (event.clientY - rect.top) * source.naturalHeight / rect.height;
  const field = document.getElementById(document.getElementById("point-target").value);
  field.value = field.value.trimEnd() + `\n${x.toFixed(3)},${y.toFixed(3)},,`;
  field.focus();
  status.textContent = "Image point added. Enter its road-plane coordinates in the point list.";
});
document.getElementById("add-feature").addEventListener("click", () => {
  try {
    const field = document.getElementById("features");
    const features = JSON.parse(field.value);
    let number = 1;
    while (features.some(feature => feature.feature_id === `feature-${number}`)) number += 1;
    features.push({feature_id: `feature-${number}`, name: document.getElementById("feature-name").value,
      kind: document.getElementById("feature-kind").value, points: document.getElementById("feature-points").value});
    field.value = JSON.stringify(features, null, 2);
    status.textContent = "Road feature added to the editable list. Preview before saving.";
  } catch { status.textContent = "Check the feature list JSON before adding another feature."; }
});
form.addEventListener("submit", async (event) => {
  if (event.submitter.value !== "preview") return;
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  status.textContent = "Checking calibration…";
  try {
    const fields = new FormData(form);
    fields.set("action", "preview");
    // getAttribute, not form.action: the "action" buttons shadow that property in browsers.
    const response = await fetch(form.getAttribute("action") || location.pathname, {method: "POST", body: fields, credentials: "same-origin"});
    if (!response.ok) throw new Error("Calibration refused. Check points, units, tolerance and reviewer attribution.");
    const data = await response.json();
    const doc = data.calibration;
    const png = Uint8Array.from(atob(data.overlay_png), c => c.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([png], {type: "image/png"}));
    const canvas = document.getElementById("calibration-preview");
    canvas.width = bitmap.width; canvas.height = bitmap.height;
    canvas.getContext("2d").drawImage(bitmap, 0, 0); bitmap.close();
    status.textContent = `Control RMSE ${doc.control_rmse}; validation RMSE ${doc.validation_rmse}; worst point ${doc.validation_max_error} ${doc.world_unit}. Validation ${doc.validation_passed ? "passes" : "fails"}.`;
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
});
