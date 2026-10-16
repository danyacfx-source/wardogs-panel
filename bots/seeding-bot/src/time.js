const MSK_OFFSET_MS = 3 * 60 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;

function shiftedDate(timestamp) {
  return new Date(timestamp + MSK_OFFSET_MS);
}

export function moscowParts(timestamp = Date.now()) {
  const date = shiftedDate(timestamp);
  return {
    year: date.getUTCFullYear(),
    month: date.getUTCMonth() + 1,
    day: date.getUTCDate(),
    weekday: date.getUTCDay(),
    hour: date.getUTCHours(),
    minute: date.getUTCMinutes(),
    second: date.getUTCSeconds()
  };
}

export function isInsideSeedWindow(timestamp, startMinutes, endMinutes) {
  const { hour, minute } = moscowParts(timestamp);
  const current = hour * 60 + minute;
  return current >= startMinutes && current < endMinutes;
}

function isoDate(date) {
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(
    date.getUTCDate()
  ).padStart(2, '0')}`;
}

export function weekKey(timestamp = Date.now()) {
  const local = shiftedDate(timestamp);
  const daysFromMonday = (local.getUTCDay() + 6) % 7;
  const monday = new Date(
    Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate()) - daysFromMonday * DAY_MS
  );
  return isoDate(monday);
}

export function previousWeekKey(timestamp = Date.now()) {
  return weekKey(timestamp - 7 * DAY_MS);
}

export function latestReportableWeekKey(timestamp, reportMinutes) {
  const parts = moscowParts(timestamp);
  const currentMinutes = parts.hour * 60 + parts.minute;
  if (parts.weekday === 0 && currentMinutes >= reportMinutes) return weekKey(timestamp);
  return previousWeekKey(timestamp);
}

export function isWeeklyReportMoment(timestamp, reportMinutes) {
  const parts = moscowParts(timestamp);
  return parts.weekday === 0 && parts.hour * 60 + parts.minute >= reportMinutes;
}

export function moscowDateKey(timestamp = Date.now()) {
  return isoDate(shiftedDate(timestamp));
}

export function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours} ч ${String(minutes).padStart(2, '0')} мин`;
  return `${minutes} мин`;
}

export function discordTimestamp(epochMs, style = 'f') {
  return `<t:${Math.floor(epochMs / 1000)}:${style}>`;
}

