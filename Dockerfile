# signal-mcp Docker image
# Bundles signal-cli (Java) + signal-mcp (Python) in a single container.
#
# Usage:
#   docker compose up
#
# First-time account linking (run once):
#   docker compose run --rm signal-mcp signal-cli link --name "MyServer"
#   # Scan the QR code on your phone, then Ctrl+C when done.
#
# After linking, start the MCP server:
#   docker compose up -d

FROM eclipse-temurin:21-jre-bookworm AS base

# ── system deps ────────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ── signal-cli ─────────────────────────────────────────────────────────────────
ARG SIGNAL_CLI_VERSION=0.14.3
RUN curl -fsSL \
    "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}-Linux-native.tar.gz" \
    | tar -xz -C /usr/local/bin --strip-components=1 signal-cli-${SIGNAL_CLI_VERSION}-Linux-native/bin/signal-cli \
 && chmod +x /usr/local/bin/signal-cli

# ── signal-mcp ─────────────────────────────────────────────────────────────────
WORKDIR /app
COPY pyproject.toml uv.lock* ./
COPY src/ ./src/

RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -e .

ENV PATH="/opt/venv/bin:$PATH"

# ── runtime ───────────────────────────────────────────────────────────────────
# Signal data is mounted at /data — map to the default signal-cli location.
ENV SIGNAL_CLI_DATA_PATH=/data/signal-cli
ENV HOME=/data

VOLUME ["/data"]

ENTRYPOINT ["signal-mcp"]
CMD ["serve"]
