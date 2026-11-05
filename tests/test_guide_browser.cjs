// node test_guide_browser.cjs /path/to/playwright
// The station's row on the guide, Watch from it onto the Watch page, and every channel of the
// lineup on the grid with or without listings, against a mocked API.
const { chromium } = require(process.argv[2] || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const now = Math.floor(Date.now() / 1000);
const hour = 3600;
const game = {start: now - hour, duration: 2 * hour, kind: 'game', title: 'Philadelphia 76ers at Detroit Pistons',
  live: true, estimated: true, episode_title: 'NBA · YouTube TV · ESPN',
  description: 'On until its final, and that time is an estimate.'};
const offAir = {start: now + hour, duration: hour, kind: 'off_air', title: 'OFF AIR', description: 'The test card.'};
const news = {start: now + 2 * hour, duration: hour / 2, kind: 'block', title: 'News 10',
  episode_title: 'YouTube TV · NBC', description: 'From the weekly schedule.'};
const house = (number, section) => ({key: `vizelvision/${number}`, kind: 'vizelvision', source_id: 'vizelvision',
  channel_id: null, name: 'VizelVision', tv_number: number, tv_name: 'VizelVision', tv_section: section,
  stream_path: '/stream/vizelvision/index.m3u8', events: [game, offAir, news]});
const antenna = {key: '7.1', kind: 'digital', number: '7.1', id: 'wjla_7_1', name: 'WJLA', tv_number: 17, tv_name: 'WJLA',
  tv_section: 'Washington & Baltimore', events: [{start: now - 600, duration: hour, title: 'World News Tonight', captions: false}]};
const summary = {available: true, paused: false, pause_reason: null, generated_at: now, last_error: null};
const views = [{tv_number: 0, tv_name: 'VizelVision', tv_section: 'Our channels'},
  {tv_number: 70, tv_name: 'VizelVision', tv_section: 'House channel'}];

const onDial = (number, id, kind, name, section, fields = {}) => ({id, source_id: id.split('/')[0],
  channel_id: id.split('/')[1], number, name, section, kind, tune: null, stream: `/stream/${id}/index.m3u8`,
  listings: null, detail: {}, ...fields});
const stationEntry = number => onDial(number, 'vizelvision/x', 'vizelvision', 'VizelVision', number ? 'House channel' : 'Our channels',
  {id: 'vizelvision', channel_id: null, listings: `vizelvision/${number}`});
const DIAL = [stationEntry(0), stationEntry(70),
  onDial(2, 'internet/pentulive', 'internet', 'pen2live', 'Our channels', {detail: {source_name: 'Pentulive', note: "Yle Areena's puppy cam"}}),
  onDial(6, 'vizelvision/sports-guide', 'slides', 'VizelVision Sports Guide', 'Our channels', {detail: {page_path: 'sports-guide.html'}}),
  onDial(15, 'digital/wttg_5_1', 'digital', 'WTTG', 'Washington & Baltimore',
    {source_id: 'digital', channel_id: 'wttg_5_1', listings: '5.1', detail: {source_name: 'WTTG-HD', antenna_number: '5.1'}}),
  onDial(17, 'digital/wjla_7_1', 'digital', 'WJLA', 'Washington & Baltimore',
    {source_id: 'digital', channel_id: 'wjla_7_1', listings: '7.1', detail: {source_name: 'WJLA', antenna_number: '7.1'}}),
  onDial(71, 'nhl/401', 'roku', 'Senators at Sabres', 'Live Sports', {listings: 'games/71'}),
  onDial(109, 'roku-manual/manual', 'roku', 'Manual Roku', 'Roku'),
  onDial(123, 'youtube-tv/Local Now', 'roku', 'Local Now', 'News', {listings: 'youtube-tv/Local Now'})];

async function open(browser, prefix, url, guide, dial = []) {
  const page = await browser.newPage({viewport: {width: 1280, height: 800}});
  const log = {errors: [], posts: [], bodies: []};
  page.on('pageerror', e => log.errors.push(e.message));
  await page.route('http://tv.test/**', async route => {
    const req = route.request(), u = new URL(req.url()), file = u.pathname.slice(prefix.length);
    if (req.method() === 'POST') { log.posts.push(file); log.bodies.push(req.postDataJSON()); return route.fulfill({json: {ok: true}}); }
    let data;
    if (file === 'api/guide') {
      const channel = u.searchParams.get('channel');
      data = channel ? {channels: {[channel]: guide.channels[channel]}} : guide;
    } else if (file === 'api/guide/now') {
      data = {channels: {'vizelvision/0': {now: game, next: offAir}, 'vizelvision/70': {now: game, next: offAir}}};
    } else if (file === 'api/v1/status') data = {api_version: 'v1', stream: {active: false}, lanes: {}, locks: {}};
    else if (file === 'api/v1/channels') {
      if (dial === null) return route.fulfill({status: 503, json: {error: 'down'}});
      data = {api_version: 'v1', channels: dial};
    } else if (file === 'api/v1/sources') data = {sources: [], lineup: {house: views,
      internet: [{tv_number: 2, tv_name: 'pen2live', tv_section: 'Our channels', channel_id: 'pentulive', name: 'pen2live', stream_path: '/stream/internet/pentulive/index.m3u8'}],
      slides: [{tv_number: 6, tv_name: 'VizelVision Sports Guide', tv_section: 'Our channels', channel_id: 'sports-guide', page_path: 'sports-guide.html'}]}};
    else if (file === 'api/v1/sources/digital' || file === 'api/v1/sources/cable') data = {items: [], source: {available: false}};
    else if (file === 'api/v1/tuner/lock') data = {locks: {}};
    if (data) return route.fulfill({json: data});
    if (file === 'hls.min.js') return route.fulfill({contentType: 'text/javascript', body: `
      window.loadedStreams = [];
      window.Hls = class {
        static isSupported() {return true} static Events = {MANIFEST_PARSED: 'parsed', ERROR: 'error'};
        static ErrorTypes = {MEDIA_ERROR: 'media'};
        constructor() {this.handlers = {}} on(e, f) {this.handlers[e] = f} loadSource(u) {window.loadedStreams.push(u)}
        attachMedia() {this.handlers.parsed?.()} destroy() {} stopLoad() {} recoverMediaError() {}
      };`});
    if (file === 'live-player.js') return route.fulfill({contentType: 'text/javascript', body: `
      window.LivePlayer = {livePosition: () => null, prepare: (m, o) => {o.onCanPlay(); o.onReady(); return () => {}}};`});
    const asset = path.join(__dirname, '..', 'static', file || 'index.html');
    if (fs.existsSync(asset) && fs.statSync(asset).isFile()) return route.fulfill({body: fs.readFileSync(asset),
      contentType: asset.endsWith('.html') ? 'text/html' : asset.endsWith('.js') ? 'text/javascript' : 'text/css'});
    return route.fulfill({status: 404, body: 'missing'});
  });
  await page.goto('http://tv.test' + prefix + url);
  return {page, log};
}

const rows = page => page.locator('.row').evaluateAll(all => all.map(row => row.dataset.key));

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const prefix of ['/tv/', '/']) {
      const guide = {available: true, channels: {'7.1': antenna, 'vizelvision/0': house(0, 'Our channels'),
        'vizelvision/70': house(70, 'House channel')}, vizelvision: summary};
      const {page, log} = await open(browser, prefix, 'guide.html', guide);
      await page.locator('.row').first().waitFor();

      // At its numbers among the lineup's, and where the guide opens with nothing tuned.
      assert.deepEqual(await rows(page), ['vizelvision/0', '7.1', 'vizelvision/70']);
      assert.equal(await page.locator('#d-chan').textContent(), '0  VizelVision');
      assert.equal(await page.locator('#d-title').textContent(), game.title);
      const sub = await page.locator('#d-sub').textContent();
      assert.match(sub, /NBA · YouTube TV · ESPN/);
      assert.match(sub, /– ~\d/, 'an estimated end says so');
      assert.match(sub, /ON NOW/);
      assert.match(sub, /LIVE/);
      assert.equal(await page.locator('#d-desc').textContent(), game.description);
      assert.equal(await page.locator('#d-watch').textContent(), 'Watch 0');
      assert.equal(await page.locator('#d-record').isVisible(), false, 'nothing here records the station');
      assert.equal(await page.locator('#btn-record-key').isDisabled(), true);

      // Its cells are the schedule's.
      const cells = page.locator('.row[data-key="vizelvision/0"] .prog');
      assert.deepEqual(await cells.locator('.prog-title').allTextContents(), [game.title, 'OFF AIR']);
      await page.keyboard.press('ArrowRight');
      assert.equal(await page.locator('#d-title').textContent(), 'OFF AIR');
      assert.equal(await page.locator('#d-desc').textContent(), 'The test card.');
      assert.doesNotMatch(await page.locator('#d-sub').textContent(), /~/);

      // Antenna numbers is the antenna's own view.
      await page.locator('#opt-antenna').check();
      assert.deepEqual(await rows(page), ['7.1']);
      await page.locator('#opt-antenna').uncheck();
      assert.equal((await rows(page))[0], 'vizelvision/0');
      assert.equal(await page.locator('#meta').textContent().then(text => /VizelVision/.test(text)), false);
      assert.deepEqual(log.posts, []);

      // Up to here, no tune. Watch goes to the Watch page, which plays the relay and says what is on.
      await page.locator('.row[data-key="vizelvision/70"] .chan').click();
      await page.waitForURL(url => url.pathname === prefix);
      await page.waitForFunction(() => window.loadedStreams && window.loadedStreams.at(-1) === 'stream/vizelvision/index.m3u8');
      assert.equal(new URL(page.url()).hash, '', 'the link is used once');
      await page.locator('#bn-body .bn-title').first().waitFor();
      assert.equal(await page.locator('#bn-num').textContent(), '70');
      assert.equal(await page.locator('#bn-body .bn-title').first().textContent(), game.title);
      assert.match(await page.locator('#bn-body .bn-time').first().textContent(), /– ~\d/);
      assert.match(await page.locator('[data-channel-key="vizelvision:70"]').textContent(), /76ers/);
      assert.equal(await page.locator('[data-channel-key="vizelvision:70"]').getAttribute('aria-current'), 'true');
      assert.deepEqual(log.posts, [], 'watching the station never tunes a source');
      assert.deepEqual(log.errors, []);
      await page.close();
    }

    // Every channel of the lineup has a row, and the cursor stops at each of them.
    const everyRow = {available: true, channels: {'7.1': antenna, 'vizelvision/0': house(0, 'Our channels'),
      'youtube-tv/Local Now': {key: 'youtube-tv/Local Now', kind: 'roku', source_id: 'youtube-tv', channel_id: 'Local Now',
        name: 'Local Now', tv_number: 123, tv_name: 'Local Now', tv_section: 'News', listing: null, events: []},
      'games/90': {key: 'games/90', kind: 'games', tv_number: 90, tv_name: 'Live Sports', tv_section: 'Live Sports', events: []},
      'games/91': {key: 'games/91', kind: 'games', tv_number: 91, tv_name: 'Live Sports', tv_section: 'Live Sports',
        events: [{start: now + 20 * hour, duration: hour, title: 'Tomorrow at Night', source_id: 'nhl', channel_id: '402', playable: true, date: 'later'}]}},
      vizelvision: summary};
    const every = await open(browser, '/', 'guide.html', everyRow, DIAL);
    await every.page.locator('.row').first().waitFor();
    assert.deepEqual(await rows(every.page), ['vizelvision/0', 'internet/pentulive', 'vizelvision/sports-guide', '5.1', '7.1',
      'vizelvision/70', 'games/71', 'games/91', 'roku-manual/manual', 'youtube-tv/Local Now'],
      'each number of the lineup, and another day\'s game; a number with neither has no row');
    const number = key => every.page.locator(`.row[data-key="${key}"] .chan-num`).textContent();
    assert.equal(await number('internet/pentulive'), '2');
    assert.equal(await number('vizelvision/sports-guide'), '6');
    assert.equal(await number('5.1'), '15');
    assert.equal(await number('vizelvision/70'), '70');
    const cell = key => every.page.locator(`.row[data-key="${key}"] .prog`);
    assert.equal(await cell('internet/pentulive').textContent(), 'pen2live');
    assert.equal(await cell('vizelvision/sports-guide').textContent(), 'VizelVision Sports Guide');
    assert.equal(await cell('vizelvision/70').textContent(), 'VizelVision', 'a station that has not answered is still tunable');
    assert.equal(await cell('5.1').textContent(), 'No information');
    assert.equal(await cell('games/91').textContent(), 'No information', 'nothing on this page, and the grid stays here');

    // Down from channel 0, without skipping a channel, on the same page.
    const before = await every.page.locator('#grid-day').textContent();
    const ruler = await every.page.locator('.tick').first().textContent();
    await every.page.keyboard.press('ArrowDown');
    assert.equal(await every.page.locator('#d-chan').textContent(), '2  pen2live  · Internet');
    assert.equal(await every.page.locator('#d-title').textContent(), 'pen2live');
    assert.equal(await every.page.locator('#d-desc').textContent(), "Yle Areena's puppy cam");
    assert.equal(await every.page.locator('#d-sub').textContent(), '', 'it has no times to give');
    assert.equal(await every.page.locator('#d-watch').textContent(), 'Watch 2');
    assert.equal(await every.page.locator('#d-record').isVisible(), false);
    assert.equal(await cell('internet/pentulive').getAttribute('aria-current'), 'true');
    await every.page.keyboard.press('ArrowDown');
    assert.equal(await every.page.locator('#d-chan').textContent(), '6  VizelVision Sports Guide');
    await every.page.keyboard.press('ArrowDown');
    assert.equal(await every.page.locator('#d-chan').textContent(), '15  WTTG  · 5.1');
    assert.equal(await every.page.locator('#d-title').textContent(), 'No information');
    assert.equal(await every.page.locator('#d-record').isVisible(), false, 'no airing to set a timer by');
    assert.equal(await every.page.locator('#btn-record-key').isDisabled(), true);
    await every.page.keyboard.press('ArrowDown');
    assert.equal(await every.page.locator('#d-title').textContent(), antenna.events[0].title);
    assert.equal(await every.page.locator('#d-record').isVisible(), true, 'a listed airing still records');
    await every.page.keyboard.press('ArrowDown');
    await every.page.keyboard.press('ArrowDown');
    assert.equal(await every.page.locator('#d-chan').textContent(), '71  Live Sports');
    assert.equal(await every.page.locator('#d-watch').isVisible(), false, 'a number with no game has nothing to watch');
    await every.page.keyboard.press('ArrowDown');
    assert.equal(await every.page.locator('#d-chan').textContent(), '91  Live Sports');
    assert.equal(await every.page.locator('#grid-day').textContent(), before);
    assert.equal(await every.page.locator('.tick').first().textContent(), ruler, 'a game tomorrow does not drag the grid there');
    await every.page.keyboard.press('ArrowRight');
    assert.equal(await every.page.locator('#d-title').textContent(), 'Tomorrow at Night', 'right finds what is listed later');

    // Antenna numbers lists the antenna's channels, listed or not, by their own numbers.
    await every.page.locator('#opt-antenna').check();
    assert.deepEqual(await rows(every.page), ['5.1', '7.1']);
    assert.equal(await number('5.1'), '5.1');
    await every.page.locator('#opt-antenna').uncheck();

    // A click, and Enter, take a channel with no listings to the Watch page.
    await cell('vizelvision/sports-guide').click();
    assert.equal(await cell('vizelvision/sports-guide').getAttribute('aria-current'), 'true');
    await every.page.keyboard.press('Enter');
    await every.page.waitForURL(url => url.pathname === '/');
    await every.page.waitForFunction(() => document.querySelector('[data-channel-key="slides:sports-guide"]')?.getAttribute('aria-current') === 'true');
    assert.equal(new URL(every.page.url()).hash, '');
    assert.deepEqual(every.log.posts, []);
    assert.deepEqual(every.log.errors, []);
    await every.page.close();

    const puppies = await open(browser, '/', 'guide.html', everyRow, DIAL);
    await puppies.page.locator('.row[data-key="internet/pentulive"] .prog').click();
    await puppies.page.locator('#d-watch').click();
    await puppies.page.waitForURL(url => url.pathname === '/');
    await puppies.page.waitForFunction(() => window.loadedStreams && window.loadedStreams.at(-1) === 'stream/internet/pentulive/index.m3u8');
    assert.deepEqual(puppies.log.posts, []);
    await puppies.page.close();

    // An antenna channel with no listing tunes as a listed one does.
    const tuned = await open(browser, '/', 'guide.html', everyRow, DIAL);
    await tuned.page.locator('.row[data-key="5.1"] .chan').click();
    await tuned.page.waitForFunction(() => location.pathname === '/');
    assert.deepEqual(tuned.log.posts, ['api/tune']);
    assert.deepEqual(tuned.log.bodies.at(-1), {channel_id: 'wttg_5_1', quality: 'medium'});
    await tuned.page.close();

    // No lineup to read: what has listings still shows. An empty guide with a lineup is still a grid.
    const noLineup = await open(browser, '/', 'guide.html', everyRow, null);
    await noLineup.page.locator('.row').first().waitFor();
    assert.deepEqual(await rows(noLineup.page), ['vizelvision/0', '7.1', 'games/91', 'youtube-tv/Local Now']);
    assert.deepEqual(noLineup.log.errors, []);
    await noLineup.page.close();
    const empty = await open(browser, '/', 'guide.html', {available: false, refresh_times: ['03:00'], channels: {}}, DIAL);
    await empty.page.locator('.row').first().waitFor();
    assert.equal((await rows(empty.page)).length, DIAL.length);
    assert.equal(await empty.page.locator('#state').isVisible(), false);
    assert.deepEqual(empty.log.errors, []);
    await empty.page.close();

    // A paused Up next, and a station that has not answered: the guide says so, and the rest stands.
    const paused = await open(browser, '/', 'guide.html', {available: true, channels: {'vizelvision/0': house(0, 'Our channels')},
      vizelvision: {...summary, paused: true, pause_reason: 'Stopped by hand'}});
    await paused.page.locator('.row').first().waitFor();
    assert.match(await paused.page.locator('#meta').textContent(), /VizelVision Up next paused/);
    await paused.page.close();
    const gone = await open(browser, '/', 'guide.html', {available: true, channels: {'7.1': antenna},
      vizelvision: {...summary, available: false, generated_at: null, last_error: "VizelVision's schedule did not answer (URLError)"}});
    await gone.page.locator('.row').first().waitFor();
    assert.deepEqual(await rows(gone.page), ['7.1']);
    assert.match(await gone.page.locator('#meta').textContent(), /VizelVision schedule unavailable/);
    assert.deepEqual(gone.log.errors, []);
    await gone.page.close();
    console.log('ok');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
