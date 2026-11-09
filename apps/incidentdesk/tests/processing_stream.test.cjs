const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Dependency-free browser seam: actual shipped script, controlled EventSource and clock.
const script = fs.readFileSync(path.join(__dirname,
  '../src/incident_report/web/static/js/processing.js'), 'utf8');
function browser() {
  const handlers = {};
  const timers = [];
  const messages = [];
  let reloads = 0;
  let stream;
  class EventSource {
    static CONNECTING = 0;
    static CLOSED = 2;
    constructor(url) {
      assert.equal(url, '/reports/test-run/events');
      this.readyState = EventSource.CONNECTING;
      stream = this;
    }
    addEventListener(name, handler) { handlers[name] = handler; }
    close() { this.readyState = EventSource.CLOSED; }
  }
  const container = {
    getAttribute() { return 'test-run'; },
    appendChild(element) { messages.push(element); },
    querySelectorAll() { return []; }
  };
  vm.runInNewContext(script, {
    EventSource,
    document: {
      querySelector() { return container; },
      getElementById() { return null; },
      createElement() {
        return {
          setAttribute() {},
          set innerHTML(value) { throw new Error('HTML insertion is forbidden'); },
          textContent: ''
        };
      }
    },
    window: { location: { reload() { reloads++; } } },
    setTimeout(callback, delay) { timers.push({ callback, delay }); },
    setInterval() { return 1; },
    clearInterval() {}
  });
  return { handlers, timers, messages, get stream() { return stream; },
    get reloads() { return reloads; } };
}

// Both HTTP 403 and 404 surface as a permanently CLOSED EventSource, without data.
for (const status of [403, 404]) {
  const page = browser();
  page.stream.readyState = 2;
  page.handlers.error({ status });
  assert.equal(page.messages[0]?.textContent,
    'Lost connection to the progress stream. Reloading…');
  assert.equal(page.timers.length, 1);
  assert.equal(page.timers[0].delay, 3000);
  assert.equal(page.reloads, 0);
  page.handlers.error({ status });
  assert.equal(page.timers.length, 1, 'duplicate errors must not schedule extra reloads');
  page.timers[0].callback();
  assert.equal(page.reloads, 1);
}
const reconnecting = browser();
reconnecting.handlers.error({});
assert.equal(reconnecting.timers.length, 0, 'temporary drops use native reconnect');
assert.equal(reconnecting.messages.length, 0);
const failed = browser();
failed.handlers.error({ data: '{"error":"Analysis failed."}' });
assert.equal(failed.reloads, 1, 'server failure reloads immediately to show Retry');
console.log('processing stream recovery: passed (403, 404, reconnect, server failure)');
