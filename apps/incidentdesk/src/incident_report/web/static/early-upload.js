/* Progressive enhancement: selection bytes remain available for native fallback. */
(() => {
  "use strict";
  const form = document.querySelector('form[action="/reports"]');
  const status = document.getElementById("staging-status");
  const csrf = form?.querySelector('[name="staging_csrf"]')?.value;
  if (!form || !csrf || !window.fetch || !window.FormData) return;
  const staged = new Map();
  const controls = ["interior_files", "exterior_files"].map(name => form.elements.namedItem(name));
  for (const input of controls) {
    input.addEventListener("change", async () => {
      const selection = { files: Array.from(input.files), ids: null };
      staged.set(input.name, selection);
      status.textContent = "Preparing selected videos. You can submit at any time.";
      try {
        const ids = [];
        // Sequential upload avoids multiplying disk/network pressure for multi-POV selections.
        for (const file of selection.files) {
          if (staged.get(input.name) !== selection) return;
          const body = new FormData();
          body.append("video", file);
          const response = await fetch("/uploads/stage", {
            method: "POST", body, credentials: "same-origin",
            headers: { "X-CSRF-Token": csrf }
          });
          if (!response.ok) throw new Error("staging failed");
          const data = await response.json();
          if (typeof data.staging_id !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(data.staging_id)) {
            throw new Error("invalid staging response");
          }
          ids.push(data.staging_id);
        }
        if (staged.get(input.name) === selection) {
          selection.ids = ids;
          status.textContent = "Selected videos prepared. Ready to submit.";
        }
      } catch (_) {
        if (staged.get(input.name) === selection) {
          selection.ids = null;
          status.textContent = "Videos will upload when you submit.";
        }
      }
    });
  }
  let submitting = false;
  form.addEventListener("submit", async event => {
    if (submitting) { event.preventDefault(); return; }
    const ready = controls.filter(input => {
      const value = staged.get(input.name);
      return value?.ids?.length && value.files.length === input.files.length &&
        value.files.every((file, i) => file === input.files[i]);
    });
    if (!ready.length) return; // no JS state or an in-flight/error upload: native form
    event.preventDefault();
    const body = new FormData(form);
    for (const input of ready) {
      body.delete(input.name);
      body.set(input.name.replace("_files", "_staging_ids"), staged.get(input.name).ids.join(","));
    }
    submitting = true;
    status.textContent = "Submitting videos…";
    try {
      const response = await fetch(form.action, { method: "POST", body, credentials: "same-origin" });
      if (response.redirected && new URL(response.url).origin === location.origin) {
        location.assign(response.url);
      } else if (response.headers.get("X-Staging-Unavailable") === "1") {
        // Expired id/restart/another worker: server guarantees no intake was started.
        HTMLFormElement.prototype.submit.call(form);
      } else {
        // Don't repeat an ambiguous POST. Preserve the selection and offer native retry.
        staged.clear();
        submitting = false;
        status.textContent = "Submission could not be completed. Submit again to upload the selected files normally.";
      }
    } catch (_) {
      // A network interruption can hide a successful POST. Avoid automatic duplication.
      staged.clear();
      submitting = false;
      status.textContent = "Connection interrupted. Check Reports before submitting again.";
    }
  });
})();
