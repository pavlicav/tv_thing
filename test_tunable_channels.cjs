const assert = require('node:assert/strict');
const test = require('node:test');
const { catalog } = require('./static/tunable-channels.js');

test('one lineup retains distinct ATSC/Roku identities and the station without Roku', () => {
  const channels = [{ id: 'nbc-4', number: '4.1', name: 'NBC', rf_channel: 34 }];
  const sources = [{ id: 'youtube-tv', name: 'YouTube TV' }];
  const feeds = { 'youtube-tv': [{ id: 'NBC', name: 'NBC', aliases: ['WRC'] }] };
  const list = catalog(channels, sources, feeds, '2026-10-08');
  assert.equal(list[0].name, 'VizelVision');
  assert.equal(list.length, 3);
  assert.equal(new Set(list.map(row => row.id)).size, 3);
  assert.equal(list.filter(row => /atsc/i.test(row.search)).length, 1);
  assert.equal(list.filter(row => /wrc/i.test(row.search)).length, 1);
  assert.equal(catalog([], [], {}, null)[0].kind, 'vizelvision');
});

test('game identity includes provider and date; unavailable games remain identifiable', () => {
  const sources = [{ id: 'mlb', name: 'MLB', dated: true }];
  const feeds = { mlb: [{ id: '123', name: 'A at B', playable: false }] };
  const a = catalog([], sources, feeds, '2026-10-08')[1];
  const b = catalog([], sources, feeds, '2026-10-09')[1];
  assert.notEqual(a.id, b.id);
  assert.equal(a.item.playable, false);
  assert.equal(a.source.id, 'mlb');
});
