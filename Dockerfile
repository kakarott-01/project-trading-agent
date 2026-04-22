FROM python:3.12-slim

WORKDIR /app

# Install all provider SDKs so the image works regardless of AI_PROVIDER
# at runtime. The unused SDKs add ~50MB — acceptable for a trading bot.
RUN pip install --no-cache-dir \
    hyperliquid-python-sdk \
    anthropic \
    openai \
    google-genai \
    python-dotenv \
    aiohttp \
    requests \
    rich \
    web3

# Copy source
COPY src ./src

# API defaults — bind to loopback inside container; expose via port mapping
ENV API_HOST=0.0.0.0
ENV API_PORT=3000
EXPOSE 3000

# Healthcheck: verify the API responds (requires API_SECRET if set)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/diary')" || exit 1

ENTRYPOINT ["python", "-m", "src.main"]