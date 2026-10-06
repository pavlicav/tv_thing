const assert = require('node:assert/strict');
const test = require('node:test');
const { bufferedAhead, livePosition, prepare } = require('./static/live-player.js');

function ranges(items) {
  return { length: items.length, start: i => items[i][0], end: i => items[i][1] };
}
function media() {
  const events = new Map();
  return {
    currentTime: 0, buffered: ranges([]), seekable: ranges([]), calls: 0,
    addEventListener(event, fn) {
      if (!events.has(event)) events.set(event, new Set());
      events.get(event).add(fn);
    },
    removeEventListener(event, fn) { events.get(event)?.delete(fn); },
    emit(event) { for (const fn of [...(events.get(event) || [])]) fn(); },
    play() { this.calls++; return Promise.resolve(); },
    listenerCount() { return [...events.values()].reduce((n, group) => n + group.size, 0); },
  };
}

test('buffer ahead never counts media across a gap', () => {
  const m = media(); m.currentTime = 1; m.buffered = ranges([[0, 1.2], [2, 10]]);
  assert.ok(Math.abs(bufferedAhead(m) - 0.2) < 0.001);
  m.currentTime = 1.5;
  assert.equal(bufferedAhead(m), 0);
});

test('waits for continuous media and announces ready only after playing', () => {
  const m = media(); let ready = 0, canPlay = 0;
  const stop = prepare(m, { onCanPlay: () => canPlay++, onReady: () => ready++, onFail: () => assert.fail('failure') });
  try {
    m.buffered = ranges([[0, 0.6]]); m.emit('progress');
    assert.equal(m.calls, 0);
    m.buffered = ranges([[0, 2.4]]); m.emit('progress'); m.emit('loadeddata');
    assert.equal(m.calls, 1); assert.equal(canPlay, 1); assert.equal(ready, 0);
    m.emit('playing'); assert.equal(ready, 1); assert.equal(m.listenerCount(), 0);
  } finally { stop(); }
});

test('retuning cancels listeners and cannot start the old media', () => {
  const m = media(); const stop = prepare(m, { onReady: () => assert.fail('old generation ready'), onFail: () => assert.fail('failure') });
  stop(); m.buffered = ranges([[0, 10]]); m.emit('progress'); m.emit('playing');
  assert.equal(m.calls, 0); assert.equal(m.listenerCount(), 0);
});

test('native and hls.js resume target a safe live position', () => {
  const m = media(); assert.equal(livePosition(m), null);
  m.seekable = ranges([[100, 110]]);
  assert.equal(livePosition(m), 107.6);
  assert.equal(livePosition(m, { liveSyncPosition: 106 }), 106);
  m.seekable = ranges([[109, 110]]);
  assert.equal(livePosition(m), 109);
});

test('autoplay denial offers manual play instead of a retune', async () => {
  const m = media(); let blocked = 0, ready = 0;
  m.play = () => Promise.reject(Object.assign(new Error('denied'), { name: 'NotAllowedError' }));
  const stop = prepare(m, { onReady: () => ready++, onBlocked: () => blocked++, onFail: () => assert.fail('failure') });
  try {
    m.buffered = ranges([[0, 2.4]]); m.emit('progress');
    await Promise.resolve();
    assert.equal(blocked, 1); assert.equal(ready, 0);
    m.emit('playing'); assert.equal(ready, 1);
  } finally { stop(); }
});

test('fatal play failure is reported once and detaches listeners', async () => {
  const m = media(); let failures = 0;
  m.play = () => Promise.reject(new Error('decoder failed'));
  const stop = prepare(m, { onReady: () => assert.fail('ready'), onFail: () => failures++ });
  try {
    m.buffered = ranges([[0, 2.4]]); m.emit('progress');
    await Promise.resolve(); m.emit('progress');
    assert.equal(failures, 1); assert.equal(m.listenerCount(), 0);
  } finally { stop(); }
});

function pollHarness() {
  const fs = require('node:fs'), vm = require('node:vm');
  const html = fs.readFileSync(require('node:path').join(__dirname, 'static/index.html'), 'utf8');
  const source = html.slice(html.indexOf('    let streamPollBusy = false;'), html.indexOf('    async function tuneTv('));
  const calls = [];
  const state = { playing: 'tv', tuning: false, tuneSerial: 1, streamId: 'old', needsReconnect: false };
  const context = {
    state, setInterval() {}, liveMessage: {},
    els: { video: { pause() {}, removeAttribute() {}, load() {} } },
    destroyHls() {}, render() {}, setStatus() {}, setNowPlaying() {},
    startTvPlayback(id) { calls.push(id); },
    fetch: async () => ({ ok: true, json: async () => ({ lanes: {
      digital: { quality: 'medium', selection: { id: 'channel-two', name: 'Two' },
        stream: { id: 'new', ready: true, state: 'ready' } },
      analog: { stream: { id: 'analog', ready: true } },
    } }) }),
  };
  vm.createContext(context); vm.runInContext(source, context);
  return { context, state, calls };
}

test('external retunes reload the viewed lane, without tuning', async () => {
  const { context, state, calls } = pollHarness();
  await context.pollStream();
  assert.deepEqual(calls, ['new']);
  assert.equal(state.currentChannelId, 'channel-two');
  await context.pollStream();
  assert.deepEqual(calls, ['new']);
});

test('an in-flight local tune ignores external generation polling', async () => {
  const { context, state, calls } = pollHarness();
  state.tuning = true;
  await context.pollStream();
  assert.deepEqual(calls, []);
});

test('a late status response cannot override a newer user choice', async () => {
  const { context, state, calls } = pollHarness();
  let release;
  context.fetch = () => new Promise(resolve => { release = resolve; });
  const pending = context.pollStream();
  state.tuneSerial++;
  release({ ok: true, json: async () => assert.fail('stale response read') });
  await pending;
  assert.deepEqual(calls, []);
});
