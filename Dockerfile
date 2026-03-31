# Zoomac Sandbox Base Image
# Minimal environment for sandboxed execution of agent tasks.

FROM python:3.11-slim

# System tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    jq \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Node.js (LTS)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Python packages commonly needed by agent tasks
RUN pip install --no-cache-dir \
    requests \
    httpx \
    beautifulsoup4 \
    pandas \
    pyyaml

# Non-root user
RUN useradd -m -s /bin/bash sandbox
USER sandbox
WORKDIR /workspace

# Default entrypoint
ENTRYPOINT ["/bin/sh", "-c"]
