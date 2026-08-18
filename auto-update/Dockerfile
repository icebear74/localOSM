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
# Custom CA certificate (oda_CA) – optional, only installed when present
# Use a glob so Docker does not fail if the file is absent from build context
# --------------------------------------------------------------------------
COPY oda_CA.pe[m] /usr/local/share/ca-certificates/
RUN if [ -f /usr/local/share/ca-certificates/oda_CA.pem ]; then \
        mv /usr/local/share/ca-certificates/oda_CA.pem \
           /usr/local/share/ca-certificates/oda_CA.crt && \
        update-ca-certificates; \
    fi

# Make the updated bundle available to Python (requests, ssl) and Node.js
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

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
