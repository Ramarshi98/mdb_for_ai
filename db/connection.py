"""
db/connection.py
----------------
Single shared entry point for talking to the AtlasTrips MongoDB Atlas cluster.
Used by app.py (Streamlit), db/seed.py, and locustfile.py so there is exactly
one place that knows how to build a client / pick collection names.

Why this matters for the demo: every workload in this repo (operational
single-document reads, $search autocomplete, booking writes) goes through the
SAME client against the SAME cluster. There is no second search service to
provision, sync, or fail independently — that's the "unified workload" pitch.
"""

import os
from functools import lru_cache

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

load_dotenv()

MONGODB_ATLAS_URI = os.getenv("MONGODB_ATLAS_URI", "")
DB_NAME = os.getenv("DB_NAME", "atlastrips")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "venues")
USERS_COLLECTION_NAME = os.getenv("USERS_COLLECTION_NAME", "users")
BOOKINGS_COLLECTION_NAME = os.getenv("BOOKINGS_COLLECTION_NAME", "bookings")
SEARCH_INDEX_NAME = os.getenv("SEARCH_INDEX_NAME", "venues_autocomplete")
SEED_DOCUMENT_COUNT = int(os.getenv("SEED_DOCUMENT_COUNT", "2000"))
# Stores per-request latency/outcome samples from both app.py and locustfile.py
# so the "Behind the Scenes" Performance Scorecard can show live numbers for
# the MongoDB path while a Locust run is in progress -- one engine handles
# the operational traffic, the search traffic, AND the telemetry about both.
METRICS_COLLECTION_NAME = os.getenv("METRICS_COLLECTION_NAME", "metrics_events")
# How long a metrics sample lives before Atlas auto-expires it (TTL index).
# Keeps metrics_events from growing unbounded across repeated demo/load-test
# sessions, which would otherwise slow down the very query that powers the
# Performance Scorecard's "live" numbers.
METRICS_TTL_SECONDS = int(os.getenv("METRICS_TTL_SECONDS", str(24 * 60 * 60)))
# Prewarms the connection pool with this many connections at client creation
# instead of establishing them lazily on first use. Without a floor, the
# first burst of concurrent Locust users pays connection-establishment cost,
# which shows up as inflated p95/p99 on exactly the samples that open a load
# test -- prewarming keeps those early samples representative of steady state.
MONGODB_MIN_POOL_SIZE = int(os.getenv("MONGODB_MIN_POOL_SIZE", "20"))


@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    """Process-wide MongoClient singleton (PyMongo already pools connections
    internally, so one client per process is the recommended pattern)."""
    if not MONGODB_ATLAS_URI:
        raise RuntimeError(
            "MONGODB_ATLAS_URI is not set. Copy env.example to .env and "
            "fill in your Atlas connection string."
        )
    return MongoClient(
        MONGODB_ATLAS_URI,
        appname="AtlasTrips",
        maxPoolSize=200,
        minPoolSize=MONGODB_MIN_POOL_SIZE,
        retryWrites=True,
    )


def get_db() -> Database:
    return get_client()[DB_NAME]


def get_venues_collection() -> Collection:
    return get_db()[COLLECTION_NAME]


def get_users_collection() -> Collection:
    return get_db()[USERS_COLLECTION_NAME]


def get_bookings_collection() -> Collection:
    return get_db()[BOOKINGS_COLLECTION_NAME]


@lru_cache(maxsize=1)
def _ensure_metrics_indexes() -> None:
    """Create metrics_events indexes once per process. Both app.py and
    locustfile.py write here on every request, and the Performance Scorecard
    reads it back with a `ts` range query -- without an index that query
    degenerates into a growing collection scan as the demo runs longer. Index
    creation is idempotent, so this is safe to call unconditionally; the
    lru_cache just avoids repeating the (no-op) call on every access."""
    coll = get_db()[METRICS_COLLECTION_NAME]
    coll.create_index([("ts", -1)])
    coll.create_index("ts", name="ts_ttl", expireAfterSeconds=METRICS_TTL_SECONDS)


def get_metrics_collection() -> Collection:
    _ensure_metrics_indexes()
    return get_db()[METRICS_COLLECTION_NAME]


def ping() -> bool:
    """Quick connectivity check used by app.py's status indicator."""
    try:
        get_client().admin.command("ping")
        return True
    except Exception:
        return False
