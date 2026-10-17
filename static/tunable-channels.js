/* Shared shape for the Watch page's tunable channel list. */
(function (root) {
  function catalog(channels, sources, feeds, date) {
    const items = [{ id: 'vizelvision', kind: 'vizelvision', name: 'VizelVision',
      key: 'VV', sourceName: 'VizelVision', tag: 'LIVE', search: 'vizelvision vizel vision live station' }];
    for (const channel of channels) {
      items.push({ id: `digital:${channel.id}`, kind: 'digital', name: channel.name,
        key: channel.number, sourceName: 'ATSC', tag: 'ATSC', channel,
        search: `${channel.number} ${channel.name} ATSC digital antenna RF ${channel.rf_channel}` });
    }
    for (const source of sources) {
      for (const item of feeds[source.id] || []) {
        items.push({ id: `roku:${source.id}:${source.dated ? date + ':' : ''}${item.id}`,
          kind: 'roku', name: item.name, sourceName: source.name, tag: 'ROKU', source, item,
          search: `Roku ${source.name} ${item.name} ${(item.aliases || []).join(' ')} ${item.status || ''}` });
      }
    }
    // One alphabetical lineup; the house station stays at the top.
    return [items[0], ...items.slice(1).sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
      || a.id.localeCompare(b.id))];
  }
  const api = { catalog };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TunableChannels = api;
})(typeof window === 'object' ? window : globalThis);
