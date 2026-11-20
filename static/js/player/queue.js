// Queue items: { trackId?, playlistId?, video_id, start, end, title, channel, thumb }.

export function itemFromRow(row, source = null) {
  const tid = Number(row.id);
  return {
    trackId: Number.isFinite(tid) && tid > 0 ? tid : null,
    playlistId: row.playlist_id != null ? Number(row.playlist_id) : (source && source.playlistId) || null,
    video_id: row.video_id,
    start: row.start_seconds != null ? Number(row.start_seconds) : null,
    end: row.end_seconds != null ? Number(row.end_seconds) : null,
    title: row.title || row.video_id,
    channel: row.channel_title || '',
    thumb: row.thumbnail_url || '',
  };
}

export function itemsFromRows(rows, source = null) {
  return (rows || []).filter((r) => r && r.video_id && !(Number(r.id) < 0)).map((r) => itemFromRow(r, source));
}
