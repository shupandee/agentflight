FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"
COPY alembic.ini ./
COPY migrations ./migrations
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn agentflight.server.main:app --host 0.0.0.0 --port 8000"]
