FROM python:3.12-slim
ARG BUILD_VERSION=main
ARG BUILD_ARCH="amd64|aarch64"
LABEL io.hass.name="Stremio Telegram" \
      io.hass.description="Private Telegram video sources for Stremio" \
      io.hass.type="addon" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.source="https://github.com/HaCStream/stremio-addon"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# SQLite otherwise prefers /var/tmp, which the HA AppArmor profile cannot write.
ENV SQLITE_TMPDIR=/tmp
ENV APP_VERSION=${BUILD_VERSION}
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir /data
COPY stremio_addon ./addon
COPY generate_session.py .
VOLUME /data
EXPOSE 8000 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-m", "addon.healthcheck"]
CMD ["python", "-m", "addon"]
