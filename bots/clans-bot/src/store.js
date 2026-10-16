import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

const DATA_DIR = path.resolve(process.env.DATA_DIR?.trim() || path.join(process.cwd(), "data"));
const STATE_FILE = path.join(DATA_DIR, "state.json");

function blankState() {
  return {
    version: 1,
    setup: {},
    clans: {},
    drafts: {},
    applications: {},
    joinApplications: {},
    temporaryVoices: {},
    backup: {},
    updatedAt: new Date(0).toISOString(),
  };
}

function validateState(state) {
  if (!state || state.version !== 1) throw new Error("Unsupported state schema");
  state.joinApplications ||= {};
  for (const key of ["setup", "clans", "drafts", "applications", "joinApplications", "temporaryVoices"]) {
    if (!state[key] || typeof state[key] !== "object" || Array.isArray(state[key])) throw new Error(`Invalid state field: ${key}`);
  }
  return { ...blankState(), ...structuredClone(state) };
}

export class Store {
  #state = blankState();
  #queue = Promise.resolve();

  async load() {
    await mkdir(DATA_DIR, { recursive: true, mode: 0o700 });
    try {
      const parsed = JSON.parse(await readFile(STATE_FILE, "utf8"));
      this.#state = validateState(parsed);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
      await this.save();
    }
    return this.#state;
  }

  get state() { return this.#state; }

  snapshot() { return structuredClone(this.#state); }

  findClanByMember(member) {
    return Object.values(this.#state.clans).find((clan) =>
      Object.values(clan.roleIds).some((roleId) => member.roles.cache.has(roleId)),
    ) ?? null;
  }

  findClanByLeader(userId) {
    return Object.values(this.#state.clans).find((clan) => clan.leaderId === userId) ?? null;
  }

  async update(mutator) {
    this.#queue = this.#queue.then(async () => {
      await mutator(this.#state);
      this.#state.updatedAt = new Date().toISOString();
      await this.#writeAtomic();
    });
    await this.#queue;
    return this.#state;
  }

  async save() {
    this.#queue = this.#queue.then(() => this.#writeAtomic());
    await this.#queue;
  }

  async replace(nextState) {
    const validated = validateState(nextState);
    this.#queue = this.#queue.then(async () => {
      this.#state = validated;
      this.#state.updatedAt = new Date().toISOString();
      await this.#writeAtomic();
    });
    await this.#queue;
    return this.#state;
  }

  async #writeAtomic() {
    const temp = `${STATE_FILE}.${process.pid}.tmp`;
    await writeFile(temp, `${JSON.stringify(this.#state, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    await rename(temp, STATE_FILE);
  }
}
