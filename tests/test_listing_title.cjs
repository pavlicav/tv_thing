const assert = require('node:assert/strict');
const test = require('node:test');
const { listingTitle, namesTeams } = require('../static/listing-title.js');

test('a game is called by its teams, its league in brackets', () => {
  const game = { title: 'College Football', episode_title: 'Michigan State at UCLA' };
  assert.equal(listingTitle(game), 'Michigan State at UCLA (NCAAF)');
  assert.equal(namesTeams(game), true);
  assert.equal(listingTitle({ title: 'NFL Football', episode_title: 'Kansas City Chiefs at Seattle Seahawks' }),
    'Kansas City Chiefs at Seattle Seahawks (NFL)');
  assert.equal(listingTitle({ title: '2026 World Series', episode_title: 'Tampa Bay Rays at Milwaukee Brewers' }),
    'Tampa Bay Rays at Milwaukee Brewers (MLB)');
  assert.equal(listingTitle({ title: '2026 WNBA Finals', episode_title: 'Golden State Valkyries at Atlanta Dream' }),
    'Golden State Valkyries at Atlanta Dream (WNBA)');
  assert.equal(listingTitle({ title: 'Premier League Soccer', episode_title: 'Chelsea vs. Tottenham Hotspur' }),
    'Chelsea vs. Tottenham Hotspur (EPL)');
});

test('a sport with no short league name keeps its whole title in the brackets', () => {
  assert.equal(listingTitle({ title: "Women's College Volleyball", episode_title: 'Texas A&M at Kentucky' }),
    "Texas A&M at Kentucky (Women's College Volleyball)");
});

test('everything else keeps its own title', () => {
  // A sport's other shows, a game with no teams named, and ordinary episodes.
  assert.equal(listingTitle({ title: 'College Football Final' }), 'College Football Final');
  assert.equal(listingTitle({ title: 'College Football' }), 'College Football');
  assert.equal(listingTitle({ title: 'B1G Football in 60', episode_title: '2026: Indiana at Michigan' }),
    'B1G Football in 60');
  assert.equal(listingTitle({ title: 'WNBA Playback', episode_title: '2026 WNBA Finals, Game 3: Golden State Valkries at Atlanta Dream' }),
    'WNBA Playback');
  assert.equal(listingTitle({ title: 'The Twilight Zone', episode_title: "The Brain Center at Whipple's" }),
    'The Twilight Zone');
  assert.equal(listingTitle({ title: 'Parks and Recreation', episode_title: 'Leslie vs. April' }),
    'Parks and Recreation');
  // A Live Sports row is titled by its teams already.
  assert.equal(listingTitle({ title: 'Lions at Packers', episode_title: 'NFL Football · Sunday Ticket' }),
    'Lions at Packers');
  assert.equal(namesTeams({ title: 'Lions at Packers' }), false);
  assert.equal(listingTitle({}), 'No title');
  assert.equal(listingTitle(null), 'No title');
});
