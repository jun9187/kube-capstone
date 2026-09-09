import logging
import os

import psycopg2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cancel_stale")

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "hotel")
DB_USER = os.environ.get("DB_USER", "hotel")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "hotel")

STALE_HOLD_MINUTES = int(os.environ.get("STALE_HOLD_MINUTES", "15"))


def main():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
        connect_timeout=5,
    )
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE bookings SET status = 'cancelled', updated_at = now() "
                "WHERE status = 'pending' AND created_at < now() - %s::interval",
                (f"{STALE_HOLD_MINUTES} minutes",),
            )
            cancelled = cur.rowcount
        logger.info(
            "auto-cancelled %d stale pending booking(s) older than %d minute(s)",
            cancelled, STALE_HOLD_MINUTES,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
