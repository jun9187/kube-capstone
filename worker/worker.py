import json
import logging
import os
import random
import threading
import time

import psycopg2
import redis
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "hotel")
DB_USER = os.environ.get("DB_USER", "hotel")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "hotel")
DB_SSLMODE = os.environ.get("DB_SSLMODE", "disable")

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
BOOKING_QUEUE = "booking_jobs"

PROCESSING_DELAY_SECONDS = float(os.environ.get("PROCESSING_DELAY_SECONDS", "2"))
PAYMENT_FAILURE_RATE = float(os.environ.get("PAYMENT_FAILURE_RATE", "0.15"))
HEALTH_PORT = int(os.environ.get("PORT", "8001"))

JOBS_PROCESSED = Counter(
    "worker_jobs_processed_total", "Booking confirmation jobs processed", ["status"]
)

app = FastAPI(title="hotel-booking-worker")


def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode=DB_SSLMODE,
        connect_timeout=3,
    )


def get_redis_client() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=10)


def process_job(booking_id: int):
    conn = get_db_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, status FROM bookings WHERE id = %s", (booking_id,))
            row = cur.fetchone()
        if row is None:
            logger.warning("booking %s no longer exists, skipping", booking_id)
            return
        if row["status"] != "pending":
            logger.info("booking %s already %s, skipping", booking_id, row["status"])
            return

        # simulate payment gateway latency
        time.sleep(PROCESSING_DELAY_SECONDS)
        status = "failed" if random.random() < PAYMENT_FAILURE_RATE else "confirmed"

        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE bookings SET status = %s, updated_at = now() "
                "WHERE id = %s AND status = 'pending'",
                (status, booking_id),
            )
        logger.info("booking %s -> %s", booking_id, status)
        JOBS_PROCESSED.labels(status).inc()
    finally:
        conn.close()


def consume_forever():
    client = get_redis_client()
    logger.info("worker consumer loop started")
    while True:
        try:
            item = client.blpop(BOOKING_QUEUE, timeout=5)
            if item is None:
                continue
            _, payload = item
            booking_id = json.loads(payload)["booking_id"]
            process_job(booking_id)
        except redis.RedisError as e:
            logger.error("redis error in consumer loop: %s", e)
            time.sleep(2)
        except Exception:
            logger.exception("unexpected error processing job")


@app.on_event("startup")
def on_startup():
    thread = threading.Thread(target=consume_forever, daemon=True)
    thread.start()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    try:
        conn = get_db_connection()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
    except psycopg2.OperationalError as e:
        raise HTTPException(status_code=503, detail="database unreachable") from e

    try:
        get_redis_client().ping()
    except redis.RedisError as e:
        raise HTTPException(status_code=503, detail="redis unreachable") from e

    return {"status": "ready"}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=HEALTH_PORT)
