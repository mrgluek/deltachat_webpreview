FROM python:3.11-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && rm -rf /var/lib/apt/lists/*

# Install official prebuilt monolith CLI (supports x86_64 and aarch64)
RUN ARCH=$(uname -m) && \
    case "${ARCH}" in \
        "x86_64") M_ARCH="x86_64" ;; \
        "aarch64"|"arm64") M_ARCH="aarch64" ;; \
        *) echo "Unsupported architecture: ${ARCH}"; exit 1 ;; \
    esac && \
    curl -sSL -o /usr/local/bin/monolith "https://github.com/Y2Z/monolith/releases/download/v2.10.1/monolith-gnu-linux-${M_ARCH}" && \
    chmod +x /usr/local/bin/monolith

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV DC_DB_DIR=/app/data
ENV DB_PATH=/app/data/webpreview.db
VOLUME /app/data

CMD ["python", "-u", "bot.py", "serve"]
