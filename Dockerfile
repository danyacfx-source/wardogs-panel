FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN useradd --system --create-home --uid 10001 wardogs
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY migrations ./migrations
COPY servers.example.json .
RUN mkdir -p /app/data && chmod 0777 /app/data && chown -R wardogs:wardogs /app
# Bothost mounts /app/data as a persistent volume at runtime.  Such a volume
# is created after the image build and is owned by root, so a fixed unprivileged
# image user cannot create the SQLite file there.  Run under the platform's
# default container user to keep the persistent database writable.

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=8s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/api/ready', timeout=5)"
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --no-server-header"]
