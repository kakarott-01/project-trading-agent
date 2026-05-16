FROM python:3.12.8-slim

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
COPY algo.py ./algo.py

# API defaults — bind to loopback inside container; expose via port mapping
ENV API_HOST=0.0.0.0
ENV API_PORT=3000
ENV TRADING_DATA_DIR=/app/data
EXPOSE 3000

# Healthcheck: verify the API responds, including API_SECRET when configured.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.parse, urllib.request; port=os.getenv('API_PORT','3000'); secret=os.getenv('API_SECRET',''); url=f'http://localhost:{port}/diary' + (f'?key={urllib.parse.quote(secret)}' if secret else ''); urllib.request.urlopen(url, timeout=5)" || exit 1

ENTRYPOINT ["python", "-m", "src.main"]
