export const FIRST_HOUR_SECONDS = 3600;
export const FIRST_INTERVAL_SECONDS = 360;
export const LATER_INTERVAL_SECONDS = 180;

export function pointsForSessionSeconds(seconds) {
  const safe = Math.max(0, Math.floor(seconds));
  const first = Math.min(safe, FIRST_HOUR_SECONDS);
  const later = Math.max(0, safe - FIRST_HOUR_SECONDS);
  return Math.floor(first / FIRST_INTERVAL_SECONDS) + Math.floor(later / LATER_INTERVAL_SECONDS);
}

export function incrementalPoints(beforeSeconds, afterSeconds) {
  if (afterSeconds < beforeSeconds) throw new Error('Session time cannot go backwards');
  return pointsForSessionSeconds(afterSeconds) - pointsForSessionSeconds(beforeSeconds);
}

