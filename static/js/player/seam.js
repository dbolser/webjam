// "Preview the seam": the last ~10 s of track A, then a hard cut into track B (Phase 2 adds
// crossfades and two decks). The lane in the player bar shows A's tail | B's head with a playhead.

export const SEAM_LEAD = 10;

/** Where in A the preview starts. `duration` may be null (then the caller waits for it). */
export function tailStart(item, duration, lead = SEAM_LEAD) {
  const end = item.end != null ? item.end : duration;
  if (end == null) return null;
  return Math.max(item.start || 0, end - lead);
}

/** Playhead 0..1 across the lane: 0–.5 is A's tail, .5–1 is B's head. */
export function seamProgress(seam, time, item, duration) {
  if (!seam) return 0;
  const clamp = (v) => Math.max(0, Math.min(1, v));
  if (seam.phase === 'a') {
    const end = item && item.end != null ? item.end : duration;
    if (!end) return 0;
    return clamp((time - (end - SEAM_LEAD)) / SEAM_LEAD) * 0.5;
  }
  return 0.5 + clamp((time - (seam.bFrom || 0)) / SEAM_LEAD) * 0.5;
}
