# Quake MCS Listener: a tiny 24/7 image (Python standard library only)
FROM python:3.13-alpine

LABEL org.opencontainers.image.title="Quake MCS Listener" \
      org.opencontainers.image.description="Earthquake early warnings, reports and on-site triggers -> Home Assistant webhooks" \
      org.opencontainers.image.source="https://github.com/Juanipis/quake-alert-listener" \
      org.opencontainers.image.licenses="MIT"

# Unprivileged user; /data keeps the anonymous device identity across restarts
RUN adduser -D -h /data quake
WORKDIR /app
COPY quake_listener.py /app/quake_listener.py

USER quake
ENV PYTHONUNBUFFERED=1 \
    QUAKE_CREDENTIALS_FILE=/data/credentials.json \
    QUAKE_HTTP_HOST=0.0.0.0
VOLUME ["/data"]

# 8990/tcp: REST API (web console, Home Assistant sensors)
# 8888/udp: optional Raspberry Shake datacast (set QUAKE_SHAKE_UDP=8888)
EXPOSE 8990/tcp 8888/udp

# Healthy while at least one push source is connected
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8990/status', timeout=4)); raise SystemExit(0 if d['google_mcs']['connected'] or any(s['connected'] for s in d['sources'].values()) else 1)"

ENTRYPOINT ["python", "/app/quake_listener.py"]
