'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');

class Store {
  constructor(directory) { this.directory = directory; this.file = path.join(directory, 'donations.json'); this.queue = Promise.resolve(); this.data = null; }
  async open() {
    await fs.mkdir(this.directory, { recursive: true, mode: 0o700 });
    try { this.data = JSON.parse(await fs.readFile(this.file, 'utf8')); }
    catch (err) { if (err.code !== 'ENOENT') throw err; this.data = {
      orders: {}, carts: {}, donors: {}, payments: {}, reviews: {}, vip: {}, adjustments: {},
      panels: {}, feedOffset: 0, topDirty: false, vipRoleId: null, sponsorChannelId: null,
    }; await this.save(); }
    if (!this.data.adjustments) { this.data.adjustments = {}; await this.save(); }
  }
  async save() {
    const tmp = `${this.file}.${process.pid}.tmp`;
    await fs.writeFile(tmp, JSON.stringify(this.data, null, 2), { mode: 0o600 });
    await fs.rename(tmp, this.file);
  }
  mutate(fn) {
    const job = this.queue.then(async () => {
      const result = await fn(this.data);
      await this.save();
      return result;
    });
    this.queue = job.catch(() => {});
    return job;
  }
}
module.exports = { Store };
