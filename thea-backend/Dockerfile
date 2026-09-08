FROM python:3.12-slim

WORKDIR /app

# supervisor: process manager running the API and worker as two programs
# in this one container. Redis itself is unchanged — still the managed
# Railway Redis addon, not something this container runs.
RUN apt-get update && apt-get install -y supervisor \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY config.yaml .
COPY supervisord.conf .

# supervisord runs the API (uvicorn) and the worker (rq) together — see
# supervisord.conf for the two program definitions.
CMD ["supervisord", "-c", "/app/supervisord.conf"]
