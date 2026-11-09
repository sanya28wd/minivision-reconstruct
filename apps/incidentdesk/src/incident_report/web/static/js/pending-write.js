// Freeze the request until its result is confirmed. Retrying never creates a new intent.
function copyFields(fields) {
  const copy = new FormData();
  for (const [key, value] of fields.entries()) copy.append(key, value);
  return copy;
}
// Statuses from the app itself that prove the write handler never ran, so the intent can be released.
const NEVER_RAN = new Set([400, 403, 404, 405, 413, 422]);
function refusalMessage(response, data) {
  if (typeof data?.error === "string" && data.error) return data.error;
  if (typeof data?.detail === "string" && data.detail) return data.detail;
  return `This action was refused (HTTP ${response.status}).`;
}
export class PendingWrite {
  // Wrapped so fetch keeps `this` = window; a stored bare fetch throws "Illegal invocation" in browsers.
  constructor(fetcher = (...args) => globalThis.fetch(...args)) { this.fetcher = fetcher; this.current = null; }
  get pending() { return this.current !== null; }
  async start(url, fields) {
    if (this.pending) throw new Error("An unconfirmed action is pending. Retry it before starting another write.");
    const frozen = copyFields(fields);
    if (!frozen.get("action_id")) frozen.set("action_id", crypto.randomUUID());
    this.current = {url, fields: frozen, attempts: 0};
    return this.retry();
  }
  async retry() {
    if (!this.pending) throw new Error("No pending action to retry.");
    const retry = this.current.attempts++ > 0;
    let response;
    try {
      response = await this.fetcher(this.current.url, {
        method: "POST", body: copyFields(this.current.fields), credentials: "same-origin",
        headers: {"Accept": "application/json", ...(retry ? {"X-Native-Retry": "1"} : {})}
      });
    } catch (error) {
      // No response arrived. Keep the action pending (safe) and say plainly what happened.
      throw new Error("The request was not sent or no response arrived. Use Retry pending action; it resends the same action ID, so a saved action is never duplicated.", {cause: error});
    }
    let data = null;
    try { data = await response.json(); }
    catch { if (response.ok) throw new Error("Result was not confirmed. Retry the pending action."); }
    if (data?.detail?.write_state === "refused") data = data.detail;
    const released = !response.ok && (data?.write_state === "refused" || NEVER_RAN.has(response.status));
    if (response.ok || released) this.current = null;
    return released ? {response, data, released, message: refusalMessage(response, data)} : {response, data};
  }
}
