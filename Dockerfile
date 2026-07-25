# kalshi-bot — headless 24/7 image.
# Uses the Anthropic API provider (ANALYST_PROVIDER=claude); the Claude CLI is
# NOT installed here because it needs an interactive subscription login.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App code only (see .dockerignore for what's excluded).
COPY kalshi_bot ./kalshi_bot
COPY main.py ./

# Mount points for the SQLite DB and (optional) Kalshi PEM.
RUN mkdir -p /app/data /app/secrets

CMD ["python", "main.py"]
