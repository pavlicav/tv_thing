/* What a listing is called on the guide and the Watch page. A game's
   listing is titled by its sport ("College Football") and names the teams
   in its episode title ("Michigan State at UCLA"); a sports channel's own
   guide leads with the teams, the league after them in brackets:
   "Michigan State at UCLA (NCAAF)". Anything else keeps its own title. */
(function (root) {
  // The telecasts' titles, by league. A sport's other shows (College
  // Football Final, WNBA Playback) are not games, and keep their titles.
  const LEAGUES = [
    [/^College Football$/, 'NCAAF'],
    [/^(Men's )?College Basketball$/, 'NCAAM'],
    [/^Women's College Basketball$/, 'NCAAW'],
    [/^NFL Football$/, 'NFL'],
    [/^MLB Baseball$|^\d{4} (World Series|(American|National) League (Championship|Division) Series|MLB Wild Card Series)$/, 'MLB'],
    [/^NBA Basketball$|^\d{4} NBA (Finals|Playoffs)$/, 'NBA'],
    [/^WNBA Basketball$|^\d{4} WNBA (Finals|Playoffs)$/, 'WNBA'],
    [/^NHL Hockey$|^\d{4} Stanley Cup (Final|Playoffs)$/, 'NHL'],
    [/^Premier League Soccer$/, 'EPL'],
    [/^MLS Soccer$/, 'MLS'],
    [/^NWSL Soccer$/, 'NWSL'],
    [/^AFL Women's Premiership Football$/, 'AFLW'],
  ];
  // Any other game names its sport last (Women's College Volleyball, Liga
  // MX Soccer), and that whole title is its bracket.
  const SPORT = /\b(Football|Basketball|Baseball|Hockey|Soccer|Volleyball|Softball|Lacrosse|Rugby)$/;
  const MATCHUP = /\S (at|vs\.?|v\.?) \S/;

  function listingTitle(event) {
    const title = (event && event.title) || 'No title';
    const teams = event && event.episode_title;
    if (!teams || !MATCHUP.test(teams)) return title;
    const league = LEAGUES.find(([pattern]) => pattern.test(title));
    if (league) return `${teams} (${league[1]})`;
    return SPORT.test(title) ? `${teams} (${title})` : title;
  }

  // Whether listingTitle already named the episode, so the line under the
  // title need not repeat it.
  function namesTeams(event) {
    return listingTitle(event) !== ((event && event.title) || 'No title');
  }

  const api = { listingTitle, namesTeams };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ListingTitle = api;
})(typeof window === 'object' ? window : globalThis);
