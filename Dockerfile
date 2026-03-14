FROM python:3.11-slim

WORKDIR /app

# Install server dependencies
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

# Copy server code
COPY server/ ./server/

# Create data directory for SQLite
RUN mkdir -p /data

ENV SB_DB_PATH=/data/signal_bridge.db
ENV SB_HOST=0.0.0.0
ENV SB_PORT=8420

EXPOSE 8420

CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8420"]
