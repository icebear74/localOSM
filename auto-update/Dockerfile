FROM python:3.12-slim

LABEL org.opencontainers.image.description="localOSM planet-update image with osmium-tool, pyosmium, mapshaper and python3"

# --------------------------------------------------------------------------
# System dependencies
# --------------------------------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        osmium-tool \
        curl \
        wget \
        ca-certificates \
        nodejs \
        npm && \
    rm -rf /var/lib/apt/lists/*

# --------------------------------------------------------------------------
# Python dependencies (pyosmium provides pyosmium-up-to-date)
# --------------------------------------------------------------------------
RUN pip install --no-cache-dir osmium

# --------------------------------------------------------------------------
# mapshaper (Node.js)
# --------------------------------------------------------------------------
RUN npm install -g mapshaper && npm cache clean --force

# --------------------------------------------------------------------------
# Working directory
# --------------------------------------------------------------------------
WORKDIR /work

COPY build_karte.sh /usr/local/bin/build_karte.sh
RUN chmod +x /usr/local/bin/build_karte.sh && \
    useradd -u 1000 -m -s /bin/bash appuser && \
    chown appuser /usr/local/bin/build_karte.sh

USER 1000

ENTRYPOINT ["/usr/local/bin/build_karte.sh"]
