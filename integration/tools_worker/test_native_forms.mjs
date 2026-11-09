// R4-3: run the real page scripts against a small DOM shim that reproduces the browser behaviours
// stubs and TestClient cannot: named form controls shadow HTMLFormElement properties
// (`form.action` becomes the <select name="action"> or a RadioNodeList), and fetch rejects a foreign `this`.
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";

const JS = new URL("../../apps/incidentdesk/src/incident_report/web/static/js/", import.meta.url);
const PATH = "/reports/run-1/tools/link-1/tracks";
let counter = 0;

class FakeElement {
  constructor(id) { this.id = id; this.value = ""; this.hidden = false; this.disabled = false; this.textContent = ""; this.listeners = {}; }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  async fire(type, event) { for (const fn of this.listeners[type] ?? []) await fn(event); }
}

// A form whose `action` property is a control (as in the browser), not the action attribute.
class FakeForm extends FakeElement {
  constructor(id, {clobbered, attribute = null, fields = []}) {
    super(id);
    this.action = clobbered;
    this.attribute = attribute;
    this.fields = fields;
    this.elements = {action_id: {value: "initial-id"}};
  }
  getAttribute(name) { return name === "action" ? this.attribute : null; }
}
const selectControl = {toString: () => "[object HTMLSelectElement]"};
const radioNodeList = {toString: () => "[object RadioNodeList]"};

function install(elements, requests, respond) {
  const RealFormData = globalThis.FormData;
  globalThis.FormData = class extends RealFormData {
    constructor(form) { super(); for (const [k, v] of form?.fields ?? []) this.append(k, v); }
  };
  const registry = new Map(elements.map(element => [element.id, element]));
  globalThis.document = {
    getElementById: id => registry.get(id) ?? null,
    querySelector: selector => (selector === "form[data-native-write]" ? elements.find(e => e instanceof FakeForm) : null),
  };
  globalThis.location = {pathname: PATH, assign: url => { requests.assigned = url; }};
  globalThis.fetch = function browserLikeFetch(url, options) {
    if (this !== undefined && this !== globalThis) throw new TypeError("Illegal invocation");
    requests.push({url: String(url), body: options?.body});
    return Promise.resolve(respond());
  };
  return () => { globalThis.FormData = RealFormData; delete globalThis.document; delete globalThis.location; };
}
const settle = () => new Promise(resolve => setTimeout(resolve, 30));

async function loadNativeWrite({form, status, retry, respond}) {
  const requests = [];
  const restore = install([form, status, retry], requests, respond);
  await import(new URL(`native-write.js?case=${counter++}`, JS));
  return {requests, restore};
}

for (const [name, clobbered, id] of [["tracks form (action select)", selectControl, "tracks-form"],
                                     ["calibration form (action buttons)", radioNodeList, "calibration-form"]]) {
  test(`R4-1: native-write posts to the page path despite a shadowed form.action: ${name}`, async () => {
    const form = new FakeForm(id, {clobbered, fields: [["reviewer", "R"], ["action", "correct"]]});
    const status = new FakeElement("native-write-status");
    const retry = new FakeElement("retry-native-write");
    const {requests, restore} = await loadNativeWrite({form, status, retry, respond: () => new Response(JSON.stringify({redirect: "/done"}), {status: 200})});
    try {
      await form.fire("submit", {submitter: {value: "save"}, preventDefault() {}});
      await settle();
      assert.equal(requests.length, 1);
      assert.equal(requests[0].url, PATH);
      assert.equal(requests.assigned, "/done");
    } finally { restore(); }
  });
}

test("R4-1: a real action attribute is still honoured", async () => {
  const form = new FakeForm("tracks-form", {clobbered: selectControl, attribute: "/custom/target", fields: [["a", "b"]]});
  const {requests, restore} = await loadNativeWrite({form, status: new FakeElement("native-write-status"), retry: new FakeElement("retry-native-write"),
    respond: () => new Response(JSON.stringify({redirect: "/done"}), {status: 200})});
  try {
    await form.fire("submit", {submitter: {value: "save"}, preventDefault() {}});
    await settle();
    assert.equal(requests[0].url, "/custom/target");
  } finally { restore(); }
});

test("R4-1: calibration preview posts to the page path despite a shadowed form.action", async () => {
  const form = new FakeForm("calibration-form", {clobbered: radioNodeList, fields: [["reviewer", "R"]]});
  const status = new FakeElement("calibration-status");
  const requests = [];
  const restore = install([form, status, new FakeElement("calibration-source"), new FakeElement("add-feature")], requests,
    () => new Response("refused", {status: 422}));
  try {
    new Function(await readFile(new URL("calibration-points.js", JS), "utf8"))();
    const button = {value: "preview", disabled: false};
    await form.fire("submit", {submitter: button, preventDefault() {}});
    await settle();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, PATH);
    assert.match(status.textContent, /Calibration refused/);
    assert.equal(button.disabled, false);
  } finally { restore(); }
});

test("R4-2: a 404 from the app clears the pending write, shows why, and allows a fresh save", async () => {
  const form = new FakeForm("tracks-form", {clobbered: selectControl, fields: [["reviewer", "R"]]});
  const status = new FakeElement("native-write-status");
  const retry = new FakeElement("retry-native-write");
  const {requests, restore} = await loadNativeWrite({form, status, retry, respond: () => new Response("<html>Not Found</html>", {status: 404})});
  try {
    await form.fire("submit", {submitter: {value: "x"}, preventDefault() {}});
    await settle();
    assert.match(status.textContent, /refused \(HTTP 404\)/);
    assert.doesNotMatch(status.textContent, /Retry the pending action/);
    assert.equal(retry.hidden, true, "no Retry for a write that never ran");
    assert.notEqual(form.elements.action_id.value, "initial-id", "a fresh action id for the next attempt");
    await form.fire("submit", {submitter: {value: "x"}, preventDefault() {}});
    await settle();
    assert.equal(requests.length, 2, "a second save is not blocked by a stale pending action");
    assert.doesNotMatch(status.textContent, /before starting another save/);
  } finally { restore(); }
});

test("R4-2: a 503 keeps the write pending and offers Retry", async () => {
  const form = new FakeForm("tracks-form", {clobbered: selectControl, fields: [["reviewer", "R"]]});
  const status = new FakeElement("native-write-status");
  const retry = new FakeElement("retry-native-write");
  const {restore} = await loadNativeWrite({form, status, retry, respond: () => new Response("down", {status: 503})});
  try {
    await form.fire("submit", {submitter: {value: "x"}, preventDefault() {}});
    await settle();
    assert.match(status.textContent, /Retry the pending action/);
    assert.equal(retry.hidden, false);
  } finally { restore(); }
});
