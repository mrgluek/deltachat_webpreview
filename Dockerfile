# --- Stage 1: Build monolith ---
FROM rust:slim-bookworm AS builder
RUN apt-get update && apt-get install -y pkg-config libssl-dev git make && rm -rf /var/lib/apt/lists/*
RUN cargo install monolith

# --- Stage 2: Runtime ---
FROM python:3.11-slim
COPY --from=builder /usr/local/cargo/bin/monolith /usr/local/bin/monolith

WORKDIR /app
RUN apt-get update && apt-get install -y git ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bin/deltachat-rpc-server /usr/local/bin/deltachat-rpc-server
COPY . .

ENV DC_DB_DIR=/app/data
ENV DB_PATH=/app/data/webpreview.db
VOLUME /app/data

CMD ["python", "-u", "bot.py", "serve"]
