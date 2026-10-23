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

test('the lineup is in TV Thing number order, with the station on both of its numbers', () => {
  const channels = [
    { id: 'shop', number: '9.5', name: 'QVC', rf_channel: 9, tv_number: 201, tv_section: 'Shopping' },
    { id: 'wrc', number: '4.1', name: 'WRC-HD', rf_channel: 34, tv_number: 4, tv_section: 'Local stations' },
    { id: 'odd', number: '99.9', name: 'Unnumbered', rf_channel: 2 }];
  const sources = [{ id: 'youtube-tv', name: 'YouTube TV' }, { id: 'sunday-ticket', name: 'NFL Sunday Ticket', dated: true }];
  const feeds = {
    'youtube-tv': [{ id: 'NBC', name: 'NBC', tv_number: 15, tv_name: 'NBC Lansing', tv_section: 'Local stations' }],
    'sunday-ticket': [{ id: '7', name: 'Bears at Falcons', tv_number: 50, tv_section: 'Football' }] };
  const lineup = { house: [{ tv_number: 0, tv_section: 'House channel' }, { tv_number: 70, tv_section: 'House channel' }] };
  const list = catalog(channels, sources, feeds, '2026-10-18', lineup);
  assert.deepEqual(list.map(row => row.number), [0, 4, 15, 50, 70, 201, null]);
  assert.deepEqual(list.map(row => row.id).slice(0, 2), ['vizelvision:0', 'digital:wrc']);
  assert.equal(list[4].id, 'vizelvision:70');
  assert.equal(list[2].name, 'NBC Lansing');
  assert.equal(list[3].section, 'Football');
  // Typing a channel number finds it; the antenna's own number still does.
  assert.ok(list[5].search.startsWith('201 ch 201 '));
  assert.ok(/\b9\.5\b/.test(list[5].search));
  // So does the lineup's name for it, and the source's own name.
  assert.ok(/\bNBC Lansing\b/.test(list[2].search) && /\bNBC\b/.test(list[2].search));
});

test('without a lineup the old alphabetical list comes back, station first', () => {
  const channels = [{ id: 'b', number: '5.1', name: 'Bravo' }, { id: 'a', number: '4.1', name: 'Alpha' }];
  const list = catalog(channels, [], {}, null, null);
  assert.deepEqual(list.map(row => row.name), ['VizelVision', 'Alpha', 'Bravo']);
  assert.equal(list[0].id, 'vizelvision');
  assert.equal(list[0].number, null);
});

test('the Sports Guide is a slide channel in its place, playing its page', () => {
  const lineup = {
    house: [{ tv_number: 0, tv_section: 'House channel' }, { tv_number: 70, tv_section: 'House channel' }],
    slides: [{ tv_number: 60, tv_name: 'VizelVision Sports Guide', tv_section: 'Live Sports',
               channel_id: 'sports-guide', page_path: 'sports-guide.html' }] };
  const sources = [{ id: 'mlb', name: 'Baseball', dated: true }];
  const feeds = { mlb: [{ id: '9', name: 'Guardians at Rays', tv_number: 61, tv_section: 'Live Sports' }] };
  const list = catalog([], sources, feeds, '2026-10-19', lineup);
  assert.deepEqual(list.map(row => row.number), [0, 60, 61, 70]);
  const guide = list[1];
  assert.equal(guide.kind, 'slides');
  assert.equal(guide.id, 'slides:sports-guide');
  assert.equal(guide.page, 'sports-guide.html');
  assert.equal(guide.name, 'VizelVision Sports Guide');
  assert.equal(guide.section, 'Live Sports');
  assert.ok(/\bsports guide\b/.test(guide.search) && guide.search.startsWith('60 ch 60 '));
  // An older server sends no slides: nothing to list.
  assert.equal(catalog([], [], {}, null, { house: lineup.house }).length, 2);
});
