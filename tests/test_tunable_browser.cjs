// node test_tunable_browser.cjs /path/to/playwright
const { chromium } = require(process.argv[2] || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const prefix of ['/tv/', '/']) {
      const page = await browser.newPage({viewport: {width: 1280, height: 900}});
      const errors = [], posts = [], unknown = [];
      page.on('pageerror', e => errors.push(e.message));
      let delayedTune, releaseTune;
      let delayTune = false, locked = false;
      const channels = [{id: 'nbc', number: '4.1', name: 'NBC', rf_channel: 34}];
      const sources = [{id: 'youtube-tv', lane: 'roku', name: 'YouTube TV', available: true},
        {id: 'mlb', lane: 'roku', name: 'MLB', dated: true, available: true},
        {id: 'roku-manual', lane: 'roku', name: 'Manual Roku', available: true}];
      const lane = {stream: {id: 'rk', ready: true, active: true}, roku: {selection: null}};
      const digital = {stream: {id: 'dtv', ready: true, active: true}, selection: channels[0], quality: 'medium'};
      const status = () => ({api_version:'v1', stream:{active:false}, lanes: {digital, roku: lane}, locks: {}});
      await page.route('http://tv.test/**', async route => {
        const req = route.request(), u = new URL(req.url());
        const file = u.pathname.slice(prefix.length);
        if (req.method() === 'POST') {
          const body = req.postDataJSON(); posts.push({file, body});
          if (file === 'api/v1/tune') {
            if (locked) return route.fulfill({status:423, json:{lock:{lane:'roku',owner:'VizelVision master',seconds_remaining:60}}});
            if (delayTune) {
              delayedTune = true;
              await new Promise(resolve => {releaseTune = resolve;});
            }
            if (body.source === 'digital') return route.fulfill({json:{ok:true, selection:channels[0],stream:digital.stream}});
            lane.roku.selection = {source_id:body.source,channel_id:body.channel_id,name:body.channel_id,
              date:body.date,manual:body.source==='roku-manual'};
            return route.fulfill({json:{ok:true,stream:lane.stream}});
          }
          return route.fulfill({json:{ok:true}});
        }
        let data;
        if (file === 'api/v1/sources/digital') data = {items:channels};
        else if (file === 'api/v1/sources/cable') data = {items:[],source:{available:false}};
        else if (file === 'api/v1/sources') data = {sources};
        else if (file === 'api/v1/sources/youtube-tv') data = {items:[{id:'NBC',name:'NBC',aliases:['WRC']}]};
        else if (file === 'api/v1/sources/mlb') data = {items:[{id:'game1',name:'Mets at Cubs',playable:true}, {id:'game2',name:'Unavailable game',playable:false}]};
        else if (file === 'api/v1/sources/roku-manual') data = {items:[{id:'manual',name:'Manual Roku'}]};
        else if (file === 'api/v1/status') data = status();
        else if (file === 'api/v1/tuner/lock') data = {locks:{}};
        else if (file === 'api/v1/roku/apps') data = {apps:[]};
        else if (file.includes('guide')) data = {channels:{}};
        if (data) return route.fulfill({json:data});
        if (file === 'hls.min.js') return route.fulfill({contentType:'text/javascript',body:`
          window.loadedStreams = []; window.instances = [];
          window.Hls = class {
            static isSupported() {return true} static Events={MANIFEST_PARSED:'parsed',ERROR:'error'};
            static ErrorTypes={MEDIA_ERROR:'media'};
            constructor(){this.handlers={};window.instances.push(this)}
            on(e,f){this.handlers[e]=f} loadSource(u){window.loadedStreams.push(u)}
            attachMedia(){this.handlers.parsed?.()} destroy(){} stopLoad(){} recoverMediaError(){}
          };`});
        if (file === 'live-player.js') return route.fulfill({contentType:'text/javascript',body:`
          window.LivePlayer={livePosition:()=>null,prepare:(m,o)=>{o.onCanPlay();o.onReady();return ()=>{}}};`});
        const asset = path.join(__dirname,'..','static',file || 'index.html');
        if (fs.existsSync(asset) && fs.statSync(asset).isFile()) return route.fulfill({
          body:fs.readFileSync(asset), contentType:asset.endsWith('.html')?'text/html':asset.endsWith('.js')?'text/javascript':'text/css'});
        unknown.push(file); return route.fulfill({status:404,body:'missing'});
      });
      await page.goto('http://tv.test' + prefix);
      const row = key => page.locator(`[data-channel-key="${key}"]`);
      await row('roku:youtube-tv:NBC').waitFor();
      assert.equal(await page.locator('#panel-count').textContent(),'6');
      assert.equal(await page.locator('#tab-tv').textContent(),'All channels');
      await page.locator('#filter').fill('NBC');
      assert.equal(await page.locator('#list .list-item').count(),2);
      await page.locator('#filter').fill('WRC');
      assert.equal(await page.locator('#list .list-item').count(),1);
      await page.locator('#filter').fill('');
      assert.equal(await page.getByRole('button',{name:/Unavailable game/}).isDisabled(),true);
      await row('digital:nbc').click();
      await page.waitForFunction(()=>window.loadedStreams.some(s=>s.includes('digital.m3u8')));
      assert.deepEqual(posts.at(-1).body,{source:'digital',channel_id:'nbc',quality:'medium'});
      await row('roku:youtube-tv:NBC').click();
      await page.waitForFunction(()=>window.loadedStreams.at(-1).includes('roku.m3u8'));
      assert.equal(posts.at(-1).body.source,'youtube-tv');
      assert.equal(await row('roku:youtube-tv:NBC').getAttribute('aria-current'),'true');
      // A retry scheduled by the old player must not replace the station.
      await page.evaluate(()=>window.instances.at(-1).handlers.error(null,{fatal:true,type:'network'}));
      const writes = posts.length;
      await row('vizelvision').click();
      await page.waitForTimeout(1200);
      assert.equal(posts.length,writes,'station viewing never tunes a source');
      assert.equal(await page.evaluate(()=>window.loadedStreams.at(-1)),'stream/vizelvision/index.m3u8');
      assert.equal(await row('roku:youtube-tv:NBC').getAttribute('aria-current'),'false');
      assert.equal(await row('vizelvision').getAttribute('aria-current'),'true');
      assert.equal(await page.locator('#quality-group').isVisible(),false);
      // An in-flight tune finishing after the station click cannot steal playback.
      delayTune = true;
      await row('digital:nbc').click();
      await page.waitForTimeout(100); assert.ok(delayedTune);
      await row('vizelvision').click(); releaseTune(); delayTune=false;
      await page.waitForTimeout(300);
      assert.equal(await page.evaluate(()=>window.loadedStreams.at(-1)),'stream/vizelvision/index.m3u8');
      // Master locks still need explicit override; declining sends no override.
      locked = true;
      await row('roku:youtube-tv:NBC').click();
      await page.locator('#ovr-cancel').click(); locked=false;
      assert.equal(posts.at(-1).body.override,undefined);
      await row('roku:roku-manual:manual').click();
      await page.locator('#roku-remote').waitFor({state:'visible'});
      await row('vizelvision').click();
      assert.equal(await page.locator('#roku-remote').isVisible(),false);
      await row('vizelvision').focus();
      await page.waitForTimeout(3300);
      assert.equal(await page.evaluate(()=>document.activeElement.dataset.channelKey),'vizelvision');
      await page.setViewportSize({width:390,height:844});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'mobile page fits');
      await page.screenshot({path:`/tmp/tv-thing-unified-${prefix==='/tv/'?'proxy':'direct'}.png`,fullPage:true});
      assert.deepEqual(errors,[]); assert.deepEqual(unknown,[]);
      await page.close();
    }
    console.log('Unified list browser checks passed: direct + /tv/, source routing, locks, station, races, focus, mobile.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
