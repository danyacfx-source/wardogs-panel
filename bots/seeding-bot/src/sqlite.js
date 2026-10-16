import Database from 'better-sqlite3';

export function openDatabase(path, { readOnly = false } = {}) {
  return new Database(path, {
    readonly: readOnly,
    fileMustExist: readOnly,
    timeout: 5000
  });
}
