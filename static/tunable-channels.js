/* Shared shape for the Watch page's tunable channel list: the TV Thing
   lineup, in channel-number order. The server numbers every channel
   (lineup.json; tv_number on each catalog item) and says which numbers are
   the station's (/api/v1/sources lineup.house) and which its slide channels
   (lineup.slides, such as the Sports Guide); this only puts them in order.
   Anything the server did not number follows, by name. */
(function (root) {
  function numbered(entry, from) {
    const number = from && Number.isInteger(from.tv_number) ? from.tv_number : null;
    entry.number = number;
    entry.section = (from && from.tv_section) || null;
    // Found by the lineup's name too (a Lansing station's call sign).
    if (from && from.tv_name) { entry.name = from.tv_name; entry.search = `${entry.search} ${from.tv_name}`; }
    if (number !== null) entry.search = `${number} ch ${number} ${entry.search}`;
    return entry;
  }

  function catalog(channels, sources, feeds, date, lineup) {
    const house = lineup && Array.isArray(lineup.house) && lineup.house.length ? lineup.house : [null];
    const items = house.map(view => numbered({
      id: view && Number.isInteger(view.tv_number) ? `vizelvision:${view.tv_number}` : 'vizelvision',
      kind: 'vizelvision', name: 'VizelVision', sourceName: 'VizelVision', tag: 'LIVE',
      search: 'vizelvision vizel vision live station' }, view));
    // The station's slide channels play a page of their own, not a stream.
    for (const view of (lineup && Array.isArray(lineup.slides) ? lineup.slides : [])) {
      if (!view || !view.page_path) continue;
      items.push(numbered({ id: `slides:${view.channel_id}`, kind: 'slides', name: view.tv_name || 'Sports Guide',
        sourceName: 'VizelVision', tag: 'GUIDE', page: view.page_path,
        sub: 'Today’s games by sport, each with its channel',
        search: 'vizelvision sports guide today games schedule listings' }, view));
    }
    for (const channel of channels) {
      items.push(numbered({ id: `digital:${channel.id}`, kind: 'digital', name: channel.name,
        sourceName: 'ATSC', tag: channel.number, channel,
        search: `${channel.number} ${channel.name} ATSC digital antenna RF ${channel.rf_channel}` }, channel));
    }
    for (const source of sources) {
      for (const item of feeds[source.id] || []) {
        items.push(numbered({ id: `roku:${source.id}:${source.dated ? date + ':' : ''}${item.id}`,
          kind: 'roku', name: item.name, sourceName: source.name, tag: 'ROKU', source, item,
          search: `Roku ${source.name} ${item.name} ${(item.aliases || []).join(' ')} ${item.status || ''}` }, item));
      }
    }
    // Without a lineup the station stays at the top of an alphabetical list.
    const rank = entry => entry.number !== null ? 0 : (entry.kind === 'vizelvision' ? -1 : 1);
    return items.sort((a, b) => rank(a) - rank(b)
      || (a.number !== null && b.number !== null ? a.number - b.number : 0)
      || a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
      || a.id.localeCompare(b.id));
  }
  const api = { catalog };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TunableChannels = api;
})(typeof window === 'object' ? window : globalThis);
