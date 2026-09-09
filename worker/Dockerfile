FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py cancel_stale.py ./

EXPOSE 8001

CMD ["python", "worker.py"]
