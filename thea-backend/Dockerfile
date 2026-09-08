FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY config.yaml .

# Default command runs the API. Override in docker-compose / Railway
# service config to run the worker instead:
#   CMD ["rq", "worker", "thea-pipeline", "--url", "$REDIS_URL"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
