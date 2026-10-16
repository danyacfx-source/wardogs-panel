'use strict';

class SiteFeed {
  constructor(url, token, fetchFn = fetch) {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:') throw new Error('SITE_BASE_URL must be HTTPS');
    this.base = parsed; this.token = token; this.fetchFn = fetchFn;
  }
  async read(offset) {
    const url = new URL('/donate/feed.php', this.base);
    url.searchParams.set('offset', String(offset));
    const response = await this.fetchFn(url, { headers: { 'X-WD-Feed-Token': this.token },
      redirect: 'error', signal: AbortSignal.timeout(12000) });
    if (!response.ok) throw new Error(`Donation feed HTTP ${response.status}`);
    const body = await response.json();
    if (!Array.isArray(body.events) || !Number.isSafeInteger(body.nextOffset) || body.nextOffset < offset || body.events.length > 100) throw new Error('Invalid donation feed');
    return body;
  }
}
module.exports = { SiteFeed };
