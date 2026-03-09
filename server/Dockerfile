FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server source + web UI
COPY *.py ./
COPY web/ ./web/

# Also need storage.py from the agent
COPY ../agent/storage.py ./

EXPOSE 8080

CMD ["python", "server.py"]
