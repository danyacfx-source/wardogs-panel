FROM node:20.20.2-bookworm-slim

ENV NODE_ENV=production \
    DATA_DIR=/app/data \
    BACKUP_DIR=/app/data/backups
WORKDIR /opt/wardogs-seeding-bot

COPY package.json package-lock.json .npmrc ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/* \
    && npm ci --omit=dev \
    && npm cache clean --force

COPY index.js ./
COPY src ./src
COPY scripts ./scripts
COPY test ./test
COPY docker-entrypoint.sh /usr/local/bin/wardogs-entrypoint

RUN mkdir -p /app/data /app/data/backups \
    && chown -R node:node /app/data \
    && chmod 700 /app/data /app/data/backups \
    && chmod 755 /usr/local/bin/wardogs-entrypoint

ENTRYPOINT ["/usr/local/bin/wardogs-entrypoint"]
CMD ["node", "index.js"]
