import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import bcrypt
import jwt
import psycopg2
import psycopg2.errors
import redis
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, EmailStr, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "hotel")
DB_USER = os.environ.get("DB_USER", "hotel")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "hotel")
DB_SSLMODE = os.environ.get("DB_SSLMODE", "disable")

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
BOOKING_QUEUE = "booking_jobs"
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "15"))

JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "60"))

REQUEST_COUNT = Counter(
    "api_requests_total", "Total requests handled", ["method", "path", "status"]
)
CACHE_HITS = Counter("api_availability_cache_hits_total", "Availability cache hits")
CACHE_MISSES = Counter("api_availability_cache_misses_total", "Availability cache misses")

app = FastAPI(title="hotel-booking-api")
bearer_scheme = HTTPBearer()

_redis_client: redis.Redis | None = None

SEED_HOTELS = [
    ("Grand Central Hotel", "Kuala Lumpur"),
    ("Seaside Resort", "Penang"),
    ("Mountain Lodge", "Cameron Highlands"),
]

SEED_ROOMS = [
    # (room_number, room_type, price_per_night, capacity)
    ("101", "Standard", Decimal("120.00"), 2),
    ("102", "Standard", Decimal("120.00"), 2),
    ("201", "Deluxe", Decimal("220.00"), 3),
    ("301", "Suite", Decimal("420.00"), 4),
]


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
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=3)
    return _redis_client


def init_db(retries: int = 10, delay: float = 2.0):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            conn = get_db_connection()
            with conn, conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS hotels (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        city TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS rooms (
                        id SERIAL PRIMARY KEY,
                        hotel_id INTEGER NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
                        room_number TEXT NOT NULL,
                        room_type TEXT NOT NULL,
                        price_per_night NUMERIC(10,2) NOT NULL,
                        capacity INTEGER NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bookings (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                        check_in DATE NOT NULL,
                        check_out DATE NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        total_price NUMERIC(10,2) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        CHECK (check_out > check_in),
                        EXCLUDE USING gist (
                            room_id WITH =,
                            daterange(check_in, check_out, '[)') WITH &&
                        ) WHERE (status IN ('pending', 'confirmed'))
                    )
                    """
                )

                cur.execute("SELECT count(*) FROM hotels")
                if cur.fetchone()[0] == 0:
                    for name, city in SEED_HOTELS:
                        cur.execute(
                            "INSERT INTO hotels (name, city) VALUES (%s, %s) RETURNING id",
                            (name, city),
                        )
                        hotel_id = cur.fetchone()[0]
                        for room_number, room_type, price, capacity in SEED_ROOMS:
                            cur.execute(
                                "INSERT INTO rooms (hotel_id, room_number, room_type, "
                                "price_per_night, capacity) VALUES (%s, %s, %s, %s, %s)",
                                (hotel_id, room_number, room_type, price, capacity),
                            )
                    logger.info("seeded %d hotels", len(SEED_HOTELS))
            conn.close()
            logger.info("database ready")
            return
        except psycopg2.OperationalError as e:
            last_err = e
            logger.warning("db not ready (attempt %d/%d): %s", attempt, retries, e)
            time.sleep(delay)
    raise RuntimeError(f"could not connect to database after {retries} attempts") from last_err


@app.on_event("startup")
def on_startup():
    init_db()


# ---------- auth helpers ----------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user_id(creds: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> int:
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail="invalid or expired token") from e


# ---------- schemas ----------


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class BookingIn(BaseModel):
    room_id: int
    check_in: date
    check_out: date


# ---------- auth routes ----------


@app.post("/api/auth/register", response_model=TokenOut, status_code=201)
def register(body: RegisterIn):
    conn = get_db_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (body.email,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="email already registered")
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                (body.email, hash_password(body.password)),
            )
            user_id = cur.fetchone()["id"]
    finally:
        conn.close()

    REQUEST_COUNT.labels("POST", "/api/auth/register", "201").inc()
    return TokenOut(access_token=create_token(user_id))


@app.post("/api/auth/login", response_model=TokenOut)
def login(body: LoginIn):
    conn = get_db_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, password_hash FROM users WHERE email = %s", (body.email,)
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None or not verify_password(body.password, row["password_hash"]):
        REQUEST_COUNT.labels("POST", "/api/auth/login", "401").inc()
        raise HTTPException(status_code=401, detail="invalid email or password")

    REQUEST_COUNT.labels("POST", "/api/auth/login", "200").inc()
    return TokenOut(access_token=create_token(row["id"]))


# ---------- hotel / room routes ----------


@app.get("/api/hotels")
def list_hotels():
    conn = get_db_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name, city FROM hotels ORDER BY name")
            rows = cur.fetchall()
    finally:
        conn.close()
    REQUEST_COUNT.labels("GET", "/api/hotels", "200").inc()
    return rows


@app.get("/api/hotels/{hotel_id}/rooms")
def list_available_rooms(hotel_id: int, check_in: date, check_out: date):
    if check_out <= check_in:
        raise HTTPException(status_code=400, detail="check_out must be after check_in")

    cache_key = f"avail:{hotel_id}:{check_in}:{check_out}"
    r = get_redis_client()
    try:
        cached = r.get(cache_key)
    except redis.RedisError:
        cached = None
    if cached is not None:
        CACHE_HITS.inc()
        REQUEST_COUNT.labels("GET", "/api/hotels/:id/rooms", "200").inc()
        return json.loads(cached)

    CACHE_MISSES.inc()
    conn = get_db_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT r.id, r.room_number, r.room_type, r.price_per_night, r.capacity
                FROM rooms r
                WHERE r.hotel_id = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM bookings b
                      WHERE b.room_id = r.id
                        AND b.status IN ('pending', 'confirmed')
                        AND daterange(b.check_in, b.check_out, '[)')
                            && daterange(%s, %s, '[)')
                  )
                ORDER BY r.price_per_night
                """,
                (hotel_id, check_in, check_out),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    result = [
        {
            "id": row["id"],
            "room_number": row["room_number"],
            "room_type": row["room_type"],
            "price_per_night": str(row["price_per_night"]),
            "capacity": row["capacity"],
        }
        for row in rows
    ]

    try:
        r.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
    except redis.RedisError as e:
        logger.warning("failed to cache availability for %s: %s", cache_key, e)

    REQUEST_COUNT.labels("GET", "/api/hotels/:id/rooms", "200").inc()
    return result


# ---------- booking routes ----------


@app.post("/api/bookings", status_code=201)
def create_booking(body: BookingIn, user_id: int = Depends(get_current_user_id)):
    if body.check_out <= body.check_in:
        raise HTTPException(status_code=400, detail="check_out must be after check_in")

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT price_per_night FROM rooms WHERE id = %s", (body.room_id,)
            )
            room = cur.fetchone()
            if room is None:
                raise HTTPException(status_code=404, detail="room not found")

            nights = (body.check_out - body.check_in).days
            total_price = room["price_per_night"] * nights

            try:
                cur.execute(
                    "INSERT INTO bookings (user_id, room_id, check_in, check_out, total_price) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "RETURNING id, room_id, check_in, check_out, status, total_price, "
                    "created_at, updated_at",
                    (user_id, body.room_id, body.check_in, body.check_out, total_price),
                )
                row = cur.fetchone()
                conn.commit()
            except psycopg2.errors.ExclusionViolation:
                conn.rollback()
                REQUEST_COUNT.labels("POST", "/api/bookings", "409").inc()
                raise HTTPException(
                    status_code=409, detail="room is not available for the selected dates"
                ) from None
    finally:
        conn.close()

    try:
        get_redis_client().rpush(BOOKING_QUEUE, json.dumps({"booking_id": row["id"]}))
    except redis.RedisError as e:
        logger.error("failed to enqueue booking %s: %s", row["id"], e)

    REQUEST_COUNT.labels("POST", "/api/bookings", "201").inc()
    return {
        "id": row["id"],
        "room_id": row["room_id"],
        "check_in": row["check_in"].isoformat(),
        "check_out": row["check_out"].isoformat(),
        "status": row["status"],
        "total_price": str(row["total_price"]),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@app.get("/api/bookings")
def list_bookings(user_id: int = Depends(get_current_user_id)):
    conn = get_db_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT b.id, b.check_in, b.check_out, b.status, b.total_price,
                       b.created_at, b.updated_at,
                       r.room_number, r.room_type, h.name AS hotel_name, h.city
                FROM bookings b
                JOIN rooms r ON r.id = b.room_id
                JOIN hotels h ON h.id = r.hotel_id
                WHERE b.user_id = %s
                ORDER BY b.id DESC
                LIMIT 200
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    REQUEST_COUNT.labels("GET", "/api/bookings", "200").inc()
    return [
        {
            "id": r["id"],
            "hotel_name": r["hotel_name"],
            "city": r["city"],
            "room_number": r["room_number"],
            "room_type": r["room_type"],
            "check_in": r["check_in"].isoformat(),
            "check_out": r["check_out"].isoformat(),
            "status": r["status"],
            "total_price": str(r["total_price"]),
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]


@app.delete("/api/bookings/{booking_id}", status_code=204)
def cancel_booking(booking_id: int, user_id: int = Depends(get_current_user_id)):
    conn = get_db_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE bookings SET status = 'cancelled', updated_at = now() "
                "WHERE id = %s AND user_id = %s AND status IN ('pending', 'confirmed')",
                (booking_id, user_id),
            )
            updated = cur.rowcount
    finally:
        conn.close()

    if updated == 0:
        raise HTTPException(status_code=404, detail="booking not found or already cancelled")
    REQUEST_COUNT.labels("DELETE", "/api/bookings/:id", "204").inc()
    return None


# ---------- ops routes ----------


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
