# Build with the repository root as context; see DOCS.md.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.24
FROM ${BUILD_FROM}

ARG BUILD_VERSION=main
ARG BUILD_ARCH=amd64
LABEL io.hass.name="Stremio Telegram" \
      io.hass.description="Private Telegram video sources for Stremio" \
      io.hass.type="addon" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.source="https://github.com/HaCStream/stremio-addon"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_VERSION=${BUILD_VERSION} \
    PATH="/opt/venv/bin:${PATH}"
WORKDIR /app
RUN apk add --no-cache python3 py3-pip \
    && python3 -m venv /opt/venv
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY stremio_addon /app/addon
COPY generate_session.py /app/generate_session.py
COPY addon/start.py /app/ha_start.py
EXPOSE 8000 8001

# Retain the HA base image's /init entrypoint (s6); it runs this command.
CMD ["python", "/app/ha_start.py"]
