FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"
EXPOSE 8000
CMD ["sh", "-c", "agentflight-server migrate && agentflight-server serve --host 0.0.0.0 --port 8000"]
