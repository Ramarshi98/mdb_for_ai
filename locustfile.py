"""
locustfile.py
=============
Load-tests the AtlasTrips DATA LAYER directly against MongoDB Atlas -- not
the Streamlit HTTP server. Locust's `HttpUser` is built for REST targets;
here we subclass the generic `User` and fire PyMongo operations ourselves,
manually reporting timings into Locust's stats via `events.request` so they
still show up in the normal Locust web UI.

Three task types run concurrently against the SAME cluster app.py uses:
  * autocomplete_search  -- Atlas Search ($search) type-ahead, weight 5
  * single_doc_read      -- high-volume find_one() venue lookups, weight 8
  * booking_write        -- atomic single-document availability update, weight 1

Every request is also logged into METRICS_COLLECTION_NAME (same collection
app.py's "Behind the Scenes -> Performance Scorecard" tab reads), so running
this load test populates the live numbers in the Streamlit demo in real time.

Usage:
    locust -f locustfile.py
    # Open http://localhost:8089, set user count + spawn rate. The "Host"
    # field is unused (we talk to Atlas via PyMongo, not HTTP) -- any value
    # works, e.g. http://localhost:8501.
"""

import random
import time
from datetime import datetime, timezone

from locust import User, between, events, task

from db.connection import (
    SEARCH_INDEX_NAME,
    get_metrics_collection,
    get_venues_collection,
)
from db.seed import AIRPORTS, HOTEL_BRANDS, LOUNGE_BRANDS

SEARCH_TERMS = (
    [a["city"] for a in AIRPORTS]
    + [a["code"] for a in AIRPORTS]
    + HOTEL_BRANDS
    + LOUNGE_BRANDS
    + ["hotel", "lounge", "event", "business", "airport"]
)

# Populated once at test start with real venue_ids so single_doc_read /
# booking_write hit documents that actually exist instead of generating
# artificial misses.
_VENUE_ID_CACHE = []


def _load_venue_id_cache(limit=5000):
    global _VENUE_ID_CACHE
    if _VENUE_ID_CACHE:
        return
    coll = get_venues_collection()
    _VENUE_ID_CACHE = [d["venue_id"] for d in coll.find({}, {"venue_id": 1}).limit(limit)]


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    _load_venue_id_cache()
    if not _VENUE_ID_CACHE:
        print(
            "WARNING: no venues found in the venues collection. "
            "Run `python -m db.seed` against your Atlas cluster before load testing."
        )
    else:
        print(f"Loaded {len(_VENUE_ID_CACHE)} venue_ids for load generation.")


def _record_and_report(environment, request_type, name, fn):
    """Run `fn`, time it, report it to Locust's stats, and append a sample
    to metrics_events -- the same collection app.py reads for its live
    Performance Scorecard."""
    start = time.perf_counter()
    exception = None
    response_length = 0
    try:
        result = fn()
        response_length = len(result) if isinstance(result, list) else 1
    except Exception as exc:  # noqa: BLE001 -- intentionally broad: report any failure to Locust
        exception = exc
    total_time_ms = (time.perf_counter() - start) * 1000

    environment.events.request.fire(
        request_type=request_type,
        name=name,
        response_time=total_time_ms,
        response_length=response_length,
        exception=exception,
        context={},
    )

    try:
        get_metrics_collection().insert_one({
            "ts": datetime.now(timezone.utc),
            "operation": name,
            "latency_ms": total_time_ms,
            "success": exception is None,
            "engine": "mongodb_atlas",
            "source": "locust",
        })
    except Exception:
        pass  # never let metrics logging affect load-test results


class AtlasTripsTrafficUser(User):
    """One simulated traveler hammering the AtlasTrips data layer at peak
    travel-booking intensity."""

    # Aggressive think time -- short pauses between actions to approximate
    # bursty peak-season booking traffic rather than a slow human pace.
    wait_time = between(0.05, 0.4)

    def on_start(self):
        _load_venue_id_cache()

    @task(5)
    def autocomplete_search(self):
        """Atlas Search $search/autocomplete -- mirrors the live type-ahead
        in app.py's Travel Search tab, including the keystroke-by-keystroke
        short-prefix queries that make autocomplete latency-sensitive."""
        term = random.choice(SEARCH_TERMS)
        prefix_len = random.randint(2, min(len(term), 6))
        query_str = term[:prefix_len]

        def run():
            coll = get_venues_collection()
            pipeline = [
                {
                    "$search": {
                        "index": SEARCH_INDEX_NAME,
                        "compound": {
                            "should": [
                                {"autocomplete": {"query": query_str, "path": "name", "fuzzy": {"maxEdits": 1}}},
                                {"autocomplete": {"query": query_str, "path": "region.city"}},
                                {"autocomplete": {"query": query_str, "path": "category"}},
                            ],
                            "minimumShouldMatch": 1,
                        },
                    }
                },
                {"$limit": 8},
                {"$project": {"_id": 0, "venue_id": 1, "name": 1, "region": 1}},
            ]
            return list(coll.aggregate(pipeline))

        _record_and_report(self.environment, "search", "autocomplete_search", run)

    @task(8)
    def single_doc_read(self):
        """High-volume operational read -- the workload that fans out into
        multiple JOINs on a normalized SQL schema but stays a single
        indexed point lookup here. Weighted highest to model "Reliability
        at Peak" / high-volume browsing traffic."""
        if not _VENUE_ID_CACHE:
            return
        venue_id = random.choice(_VENUE_ID_CACHE)

        def run():
            return get_venues_collection().find_one({"venue_id": venue_id})

        _record_and_report(self.environment, "read", "single_doc_read", run)

    @task(1)
    def booking_write(self):
        """Lower-frequency atomic write -- mirrors the Book Now flow's
        positional $ update against a single venue document (no multi-table
        transaction required)."""
        if not _VENUE_ID_CACHE:
            return
        venue_id = random.choice(_VENUE_ID_CACHE)

        def run():
            coll = get_venues_collection()
            doc = coll.find_one({"venue_id": venue_id}, {"availability_calendar.date": 1})
            if not doc or not doc.get("availability_calendar"):
                return None
            date_str = random.choice(doc["availability_calendar"])["date"]
            return coll.update_one(
                {"venue_id": venue_id, "availability_calendar.date": date_str},
                {"$inc": {"availability_calendar.$.available_units": -1}},
            )

        _record_and_report(self.environment, "write", "booking_write", run)
