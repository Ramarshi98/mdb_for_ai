"""
db/schema.py
------------
Reference shape of the two collections used in this demo. These are plain
Python dicts (not a schema-enforcing model) on purpose: the whole point of
the demo is that MongoDB's flexible document model lets "hotel," "lounge,"
and "event" venues share one collection with different optional sub-shapes,
instead of being split across normalized SQL tables (venues, addresses,
pricing_tiers, availability, facilities, ...) that need JOINs to reassemble.

VENUE_DOCUMENT_EXAMPLE and USER_DOCUMENT_EXAMPLE are illustrative -- db/seed.py
generates many documents that follow this same shape with varied, realistic
values.
"""

from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# VENUES collection
# One denormalized document per hotel / airport lounge / event venue.
# Region, pricing, availability, and facilities are all embedded so a single
# find_one() (or a single $search result) returns everything the UI needs --
# no JOIN, no second round trip, no ORM relationship loading.
# ---------------------------------------------------------------------------
VENUE_DOCUMENT_EXAMPLE = {
    "_id": "66f1a2b3c4d5e6f7a8b9c0d1",  # ObjectId in real documents
    "venue_id": "HTL-LHR-0001",
    "type": "hotel",  # "hotel" | "lounge" | "event"
    "category": "hotel",  # used by Atlas Search facet/autocomplete
    "name": "Heathrow Marriott Hotel",
    "brand": "Marriott",
    "description": (
        "Full-service airport hotel with skyline lounge, 24h fitness center, "
        "and direct shuttle to Heathrow Terminals 1-3."
    ),
    "region": {
        "continent": "Europe",
        "country": "United Kingdom",
        "country_code": "GB",
        "city": "London",
        "airport_code": "LHR",          # nearest/affiliated IATA airport
        "coordinates": {"lat": 51.4700, "lng": -0.4543},
        "timezone": "Europe/London",
    },
    "rating": 4.6,
    "review_count": 8423,
    "price_tier": "$$$",  # $ .. $$$$
    "pricing": {
        "currency": "USD",
        "base_rate": 219.00,
        "taxes_fees_pct": 0.18,
        # Per-date overrides driven by demand -- embedded, no separate
        # pricing-engine table/JOIN required to render a quote.
        "dynamic_rates": [
            {"date": "2026-06-25", "rate": 219.00, "demand": "normal"},
            {"date": "2026-06-26", "rate": 249.00, "demand": "high"},
            {"date": "2026-06-27", "rate": 199.00, "demand": "low"},
        ],
    },
    # Rolling availability calendar embedded directly on the venue document.
    # At read time the app slices this array -- no availability JOIN, no
    # separate inventory microservice call.
    "availability_calendar": [
        {"date": "2026-06-25", "total_units": 340, "available_units": 58, "status": "available"},
        {"date": "2026-06-26", "total_units": 340, "available_units": 12, "status": "limited"},
        {"date": "2026-06-27", "total_units": 340, "available_units": 0, "status": "sold_out"},
    ],
    "facilities": {
        "wifi": True,
        "pool": True,
        "spa": False,
        "gym": True,
        "meeting_rooms": 6,
        "shuttle_service": True,
        "shower_suites": None,        # lounge-specific field, null for hotels
        "max_capacity": None,         # event-specific field, null for hotels
        "amenities": ["airport_shuttle", "business_center", "restaurant", "bar"],
        "accessibility": {"wheelchair_accessible": True, "hearing_assist": True},
    },
    "images": [
        "https://images.atlastrips.demo/venues/htl-lhr-0001/lobby.jpg",
        "https://images.atlastrips.demo/venues/htl-lhr-0001/room.jpg",
    ],
    "tags": ["business", "airport-adjacent", "family-friendly"],
    # Denormalized keyword array purely to make autocomplete/relevance richer;
    # Atlas Search also indexes name/city/category directly (see
    # search/atlas_search_index.json).
    "search_keywords": ["heathrow", "marriott", "lhr", "london hotel"],
    "created_at": datetime(2025, 1, 4, tzinfo=timezone.utc),
    "updated_at": datetime(2026, 6, 20, tzinfo=timezone.utc),
}

# ---------------------------------------------------------------------------
# USERS collection
# Bookings and saved venues are embedded sub-documents on the user, which
# mirrors how a frequent traveler's profile is actually read: "show me this
# person + everything they've booked" in one query, instead of a
# users JOIN bookings JOIN venues JOIN payment_methods chain.
# ---------------------------------------------------------------------------
USER_DOCUMENT_EXAMPLE = {
    "_id": "66f1b7c8d9e0f1a2b3c4d5e6",
    "user_id": "USR-00001",
    "name": "Priya Nandakumar",
    "email": "priya.n@example.com",
    "loyalty_tier": "gold",  # standard | silver | gold | platinum
    "home_airport": "SIN",
    "preferences": {
        "preferred_class": "business",
        "language": "en",
        "dietary": ["vegetarian"],
        "marketing_opt_in": False,
    },
    "saved_venues": ["HTL-LHR-0001", "LNG-SIN-0007", "EVT-LAS-0014"],
    # Embedded booking history -- each booking is a self-contained snapshot
    # (venue name/city/rate captured at booking time) so rendering "My Trips"
    # never has to re-JOIN against the venues collection.
    "bookings": [
        {
            "booking_id": "BKG-2026-000482",
            "venue_id": "HTL-LHR-0001",
            "venue_name_snapshot": "Heathrow Marriott Hotel",
            "venue_city_snapshot": "London",
            "check_in": "2026-07-14",
            "check_out": "2026-07-17",
            "status": "confirmed",  # confirmed | pending | cancelled
            "total_price": 657.00,
            "currency": "USD",
            "booked_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        }
    ],
    "payment_methods": [
        {"type": "card", "brand": "visa", "last4": "4242", "is_default": True}
    ],
    "created_at": datetime(2024, 11, 2, tzinfo=timezone.utc),
    "updated_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
}


def sample_availability_window(days: int = 14, start: datetime | None = None):
    """Helper used by db/seed.py to generate a rolling N-day availability
    calendar for a venue, matching the shape above."""
    start = start or datetime.now(timezone.utc)
    return [
        {
            "date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
            "total_units": 0,
            "available_units": 0,
            "status": "available",
        }
        for i in range(days)
    ]
