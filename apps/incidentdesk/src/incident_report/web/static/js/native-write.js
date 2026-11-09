import {PendingWrite} from "./pending-write.js";

const form = document.querySelector("form[data-native-write]");
const status = document.getElementById("native-write-status");
const retry = document.getElementById("retry-native-write");
const pending = new PendingWrite();
let busy = false;
async function send(fields) {
  if (busy) return;
  busy = true;
  retry.disabled = true;
  status.textContent = "Saving review action…";
  try {
    // getAttribute, not form.action: a control named "action" shadows the property in browsers.
    const attempt = fields ? await pending.start(form.getAttribute("action") || location.pathname, fields) : await pending.retry();
    const {response, data} = attempt;
    if (response.ok) {
      status.textContent = "Saved. Loading the current revision…";
      location.assign(data.redirect);
    } else {
      status.textContent = attempt.message || data?.error || "Completion was not confirmed. Retry the pending action.";
      if (!pending.pending) form.elements.action_id.value = crypto.randomUUID();
    }
  } catch (error) { status.textContent = error.message; }
  finally { busy = false; retry.disabled = false; retry.hidden = !pending.pending; }
}
form.addEventListener("submit", event => {
  const action = event.submitter?.value;
  if (form.id === "calibration-form" && action !== "save") return;
  event.preventDefault();
  if (busy) return;
  if (pending.pending) {
    status.textContent = "Retry the pending action before starting another save.";
    return;
  }
  const fields = new FormData(form);
  if (form.id === "calibration-form") fields.set("action", "save");
  send(fields);
});
retry.addEventListener("click", () => send());
