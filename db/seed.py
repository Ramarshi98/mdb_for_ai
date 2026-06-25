"""
db/seed.py
----------
Generates authentic-looking, globally distributed demo data for AtlasTrips:
real IATA airports/cities, real hotel & lounge brand names, real convention
center names -- combined into denormalized venue documents (see
db/schema.py) plus a smaller set of user profiles with embedded booking
history.

Usage:
    python -m db.seed                 # seed using SEED_DOCUMENT_COUNT from .env
    python -m db.seed --count 5000    # override venue count
    python -m db.seed --reset         # drop existing collections first
"""

import argparse
import random
from datetime import datetime, timedelta, timezone

from faker import Faker

from db.connection import (
    get_users_collection,
    get_venues_collection,
)

fake = Faker()

# ---------------------------------------------------------------------------
# Real-world reference data (kept intentionally small + curated rather than
# fully random, so the demo always reads as "authentic" rather than
# obviously synthetic).
# ---------------------------------------------------------------------------
AIRPORTS = [
    {"code": "LHR", "city": "London", "country": "United Kingdom", "country_code": "GB", "continent": "Europe", "lat": 51.4700, "lng": -0.4543, "timezone": "Europe/London"},
    {"code": "JFK", "city": "New York", "country": "United States", "country_code": "US", "continent": "North America", "lat": 40.6413, "lng": -73.7781, "timezone": "America/New_York"},
    {"code": "SIN", "city": "Singapore", "country": "Singapore", "country_code": "SG", "continent": "Asia", "lat": 1.3644, "lng": 103.9915, "timezone": "Asia/Singapore"},
    {"code": "DXB", "city": "Dubai", "country": "United Arab Emirates", "country_code": "AE", "continent": "Asia", "lat": 25.2532, "lng": 55.3657, "timezone": "Asia/Dubai"},
    {"code": "HND", "city": "Tokyo", "country": "Japan", "country_code": "JP", "continent": "Asia", "lat": 35.5494, "lng": 139.7798, "timezone": "Asia/Tokyo"},
    {"code": "CDG", "city": "Paris", "country": "France", "country_code": "FR", "continent": "Europe", "lat": 49.0097, "lng": 2.5479, "timezone": "Europe/Paris"},
    {"code": "SYD", "city": "Sydney", "country": "Australia", "country_code": "AU", "continent": "Oceania", "lat": -33.9399, "lng": 151.1753, "timezone": "Australia/Sydney"},
    {"code": "GRU", "city": "Sao Paulo", "country": "Brazil", "country_code": "BR", "continent": "South America", "lat": -23.4356, "lng": -46.4731, "timezone": "America/Sao_Paulo"},
    {"code": "JNB", "city": "Johannesburg", "country": "South Africa", "country_code": "ZA", "continent": "Africa", "lat": -26.1392, "lng": 28.2460, "timezone": "Africa/Johannesburg"},
    {"code": "LAX", "city": "Los Angeles", "country": "United States", "country_code": "US", "continent": "North America", "lat": 33.9416, "lng": -118.4085, "timezone": "America/Los_Angeles"},
    {"code": "HKG", "city": "Hong Kong", "country": "Hong Kong", "country_code": "HK", "continent": "Asia", "lat": 22.3080, "lng": 113.9185, "timezone": "Asia/Hong_Kong"},
    {"code": "FRA", "city": "Frankfurt", "country": "Germany", "country_code": "DE", "continent": "Europe", "lat": 50.0379, "lng": 8.5622, "timezone": "Europe/Berlin"},
    {"code": "ORD", "city": "Chicago", "country": "United States", "country_code": "US", "continent": "North America", "lat": 41.9742, "lng": -87.9073, "timezone": "America/Chicago"},
    {"code": "YYZ", "city": "Toronto", "country": "Canada", "country_code": "CA", "continent": "North America", "lat": 43.6777, "lng": -79.6248, "timezone": "America/Toronto"},
    {"code": "ICN", "city": "Seoul", "country": "South Korea", "country_code": "KR", "continent": "Asia", "lat": 37.4602, "lng": 126.4407, "timezone": "Asia/Seoul"},
]

HOTEL_BRANDS = [
    "Marriott", "Hilton", "Hyatt Regency", "InterContinental", "Four Seasons",
    "Ritz-Carlton", "Westin", "Sheraton", "Conrad", "W Hotels",
    "Park Hyatt", "Le Meridien", "Andaz", "JW Marriott", "St. Regis",
]

LOUNGE_BRANDS = [
    "Centurion Lounge", "Plaza Premium Lounge", "The Pier Business Class Lounge",
    "Cathay Pacific Lounge", "Star Alliance Lounge", "Emirates Lounge",
    "Qantas Club", "SkyTeam Lounge", "Virgin Atlantic Clubhouse",
    "British Airways Galleries Lounge",
]

EVENT_HUBS = [
    "ExCeL London", "Javits Center", "Marina Bay Sands Expo",
    "Las Vegas Convention Center", "Messe Frankfurt", "Dubai World Trade Centre",
    "Sydney International Convention Centre", "Moscone Center",
    "Hong Kong Convention and Exhibition Centre", "Suntec Singapore",
]

EVENT_TOPICS = [
    "Global Travel & Tourism Summit", "Future of Aviation Expo",
    "International Hospitality Forum", "World Logistics Congress",
    "Cloud & Data Infrastructure Conference", "Fintech Innovators Summit",
]

AMENITIES_POOL = [
    "airport_shuttle", "business_center", "restaurant", "bar", "rooftop_bar",
    "valet_parking", "ev_charging", "co_working_space", "kids_club",
]

PRICE_TIERS = ["$", "$$", "$$$", "$$$$"]


def _dynamic_rates(base_rate: float, days: int = 7):
    today = datetime.now(timezone.utc).date()
    rates = []
    for i in range(days):
        demand = random.choices(["low", "normal", "high"], weights=[0.25, 0.5, 0.25])[0]
        multiplier = {"low": 0.85, "normal": 1.0, "high": 1.25}[demand]
        rates.append({
            "date": (today + timedelta(days=i)).isoformat(),
            "rate": round(base_rate * multiplier, 2),
            "demand": demand,
        })
    return rates


def _availability_calendar(total_units: int, days: int = 7):
    calendar = []
    today = datetime.now(timezone.utc).date()
    for i in range(days):
        available = max(0, total_units - random.randint(0, total_units))
        status = "sold_out" if available == 0 else ("limited" if available < total_units * 0.15 else "available")
        calendar.append({
            "date": (today + timedelta(days=i)).isoformat(),
            "total_units": total_units,
            "available_units": available,
            "status": status,
        })
    return calendar


def _make_hotel(idx: int, airport: dict) -> dict:
    brand = random.choice(HOTEL_BRANDS)
    base_rate = round(random.uniform(89, 650), 2)
    total_units = random.randint(120, 450)
    return {
        "venue_id": f"HTL-{airport['code']}-{idx:04d}",
        "type": "hotel",
        "category": "hotel",
        "name": f"{airport['city']} {brand}",
        "brand": brand,
        "description": (
            f"{brand} property near {airport['city']} ({airport['code']}) offering "
            f"{random.choice(['skyline views', 'airport shuttle service', 'a rooftop pool', 'executive lounge access'])} "
            f"and {random.randint(80, 600)} guest rooms."
        ),
        "region": {
            "continent": airport["continent"],
            "country": airport["country"],
            "country_code": airport["country_code"],
            "city": airport["city"],
            "airport_code": airport["code"],
            "coordinates": {"lat": airport["lat"] + random.uniform(-0.08, 0.08), "lng": airport["lng"] + random.uniform(-0.08, 0.08)},
            "timezone": airport["timezone"],
        },
        "rating": round(random.uniform(3.6, 4.9), 1),
        "review_count": random.randint(120, 12000),
        "price_tier": random.choice(PRICE_TIERS),
        "pricing": {
            "currency": "USD",
            "base_rate": base_rate,
            "taxes_fees_pct": round(random.uniform(0.08, 0.22), 2),
            "dynamic_rates": _dynamic_rates(base_rate),
        },
        "availability_calendar": _availability_calendar(total_units),
        "facilities": {
            "wifi": True,
            "pool": random.random() > 0.4,
            "spa": random.random() > 0.6,
            "gym": random.random() > 0.2,
            "meeting_rooms": random.randint(0, 12),
            "shuttle_service": random.random() > 0.3,
            "shower_suites": None,
            "max_capacity": None,
            "amenities": random.sample(AMENITIES_POOL, k=random.randint(3, 6)),
            "accessibility": {"wheelchair_accessible": True, "hearing_assist": random.random() > 0.5},
        },
        "images": [f"https://images.atlastrips.demo/venues/htl-{airport['code'].lower()}-{idx:04d}/lobby.jpg"],
        "tags": random.sample(["business", "airport-adjacent", "family-friendly", "luxury", "budget"], k=2),
        "search_keywords": [airport["city"].lower(), brand.lower(), airport["code"].lower(), "hotel"],
        "created_at": datetime.now(timezone.utc) - timedelta(days=random.randint(30, 700)),
        "updated_at": datetime.now(timezone.utc) - timedelta(days=random.randint(0, 14)),
    }


def _make_lounge(idx: int, airport: dict) -> dict:
    brand = random.choice(LOUNGE_BRANDS)
    base_rate = round(random.uniform(35, 95), 2)
    total_units = random.randint(40, 180)
    return {
        "venue_id": f"LNG-{airport['code']}-{idx:04d}",
        "type": "lounge",
        "category": "airport_lounge",
        "name": f"{brand} - {airport['code']}",
        "brand": brand,
        "description": (
            f"{brand} at {airport['city']} {airport['code']} with "
            f"{random.choice(['à la carte dining', 'shower suites', 'quiet pods', 'a full bar'])} "
            f"for travelers in transit."
        ),
        "region": {
            "continent": airport["continent"],
            "country": airport["country"],
            "country_code": airport["country_code"],
            "city": airport["city"],
            "airport_code": airport["code"],
            "coordinates": {"lat": airport["lat"], "lng": airport["lng"]},
            "timezone": airport["timezone"],
        },
        "rating": round(random.uniform(3.8, 4.95), 1),
        "review_count": random.randint(60, 5000),
        "price_tier": random.choice(PRICE_TIERS[:3]),
        "pricing": {
            "currency": "USD",
            "base_rate": base_rate,
            "taxes_fees_pct": round(random.uniform(0.0, 0.1), 2),
            "dynamic_rates": _dynamic_rates(base_rate),
        },
        "availability_calendar": _availability_calendar(total_units),
        "facilities": {
            "wifi": True,
            "pool": False,
            "spa": False,
            "gym": False,
            "meeting_rooms": random.randint(0, 3),
            "shuttle_service": False,
            "shower_suites": random.random() > 0.5,
            "max_capacity": None,
            "amenities": random.sample(AMENITIES_POOL, k=random.randint(2, 4)),
            "accessibility": {"wheelchair_accessible": True, "hearing_assist": False},
        },
        "images": [f"https://images.atlastrips.demo/venues/lng-{airport['code'].lower()}-{idx:04d}/lounge.jpg"],
        "tags": random.sample(["business", "quiet", "family-friendly", "premium"], k=2),
        "search_keywords": [airport["city"].lower(), brand.lower(), airport["code"].lower(), "lounge"],
        "created_at": datetime.now(timezone.utc) - timedelta(days=random.randint(30, 700)),
        "updated_at": datetime.now(timezone.utc) - timedelta(days=random.randint(0, 14)),
    }


def _make_event(idx: int, airport: dict) -> dict:
    hub = random.choice(EVENT_HUBS)
    topic = random.choice(EVENT_TOPICS)
    base_rate = round(random.uniform(199, 1499), 2)
    total_units = random.randint(500, 20000)
    start = datetime.now(timezone.utc) + timedelta(days=random.randint(5, 180))
    return {
        "venue_id": f"EVT-{airport['code']}-{idx:04d}",
        "type": "event",
        "category": "event",
        "name": f"{topic} {start.year} - {airport['city']}",
        "brand": hub,
        "description": f"{topic} hosted at {hub} in {airport['city']}, {start.strftime('%B %Y')}.",
        "region": {
            "continent": airport["continent"],
            "country": airport["country"],
            "country_code": airport["country_code"],
            "city": airport["city"],
            "airport_code": airport["code"],
            "coordinates": {"lat": airport["lat"], "lng": airport["lng"]},
            "timezone": airport["timezone"],
        },
        "rating": round(random.uniform(3.9, 4.9), 1),
        "review_count": random.randint(20, 900),
        "price_tier": random.choice(PRICE_TIERS),
        "pricing": {
            "currency": "USD",
            "base_rate": base_rate,
            "taxes_fees_pct": round(random.uniform(0.05, 0.15), 2),
            "dynamic_rates": _dynamic_rates(base_rate),
        },
        "availability_calendar": _availability_calendar(total_units),
        "facilities": {
            "wifi": True,
            "pool": False,
            "spa": False,
            "gym": False,
            "meeting_rooms": random.randint(5, 40),
            "shuttle_service": random.random() > 0.5,
            "shower_suites": None,
            "max_capacity": total_units,
            "amenities": random.sample(AMENITIES_POOL, k=random.randint(2, 5)),
            "accessibility": {"wheelchair_accessible": True, "hearing_assist": True},
        },
        "images": [f"https://images.atlastrips.demo/venues/evt-{airport['code'].lower()}-{idx:04d}/hall.jpg"],
        "tags": ["conference", "networking"],
        "search_keywords": [airport["city"].lower(), hub.lower(), topic.lower(), "event"],
        "event_start": start.date().isoformat(),
        "event_end": (start + timedelta(days=random.randint(1, 4))).date().isoformat(),
        "created_at": datetime.now(timezone.utc) - timedelta(days=random.randint(10, 200)),
        "updated_at": datetime.now(timezone.utc) - timedelta(days=random.randint(0, 14)),
    }


def generate_venues(count: int) -> list[dict]:
    """Generate `count` venue documents, roughly 50% hotels / 25% lounges / 25% events,
    spread across all curated airports/regions."""
    venues = []
    for i in range(1, count + 1):
        airport = random.choice(AIRPORTS)
        roll = random.random()
        if roll < 0.50:
            venues.append(_make_hotel(i, airport))
        elif roll < 0.75:
            venues.append(_make_lounge(i, airport))
        else:
            venues.append(_make_event(i, airport))
    return venues


def generate_users(count: int, venues: list[dict]) -> list[dict]:
    """Generate `count` user profiles with embedded booking history referencing
    real generated venue_ids -- demonstrating embedded one-to-many data that
    would otherwise require a bookings JOIN table in SQL."""
    users = []
    venue_ids = [v["venue_id"] for v in venues]
    for i in range(1, count + 1):
        name = fake.name()
        num_bookings = random.randint(0, 4)
        bookings = []
        for _ in range(num_bookings):
            venue = random.choice(venues)
            check_in = datetime.now(timezone.utc) + timedelta(days=random.randint(-60, 120))
            bookings.append({
                "booking_id": f"BKG-{check_in.year}-{random.randint(100000, 999999)}",
                "venue_id": venue["venue_id"],
                "venue_name_snapshot": venue["name"],
                "venue_city_snapshot": venue["region"]["city"],
                "check_in": check_in.date().isoformat(),
                "check_out": (check_in + timedelta(days=random.randint(1, 6))).date().isoformat(),
                "status": random.choice(["confirmed", "confirmed", "pending", "cancelled"]),
                "total_price": round(venue["pricing"]["base_rate"] * random.randint(1, 5), 2),
                "currency": "USD",
                "booked_at": check_in - timedelta(days=random.randint(1, 45)),
            })
        users.append({
            "user_id": f"USR-{i:05d}",
            "name": name,
            "email": fake.email(),
            "loyalty_tier": random.choice(["standard", "silver", "gold", "platinum"]),
            "home_airport": random.choice(AIRPORTS)["code"],
            "preferences": {
                "preferred_class": random.choice(["economy", "premium_economy", "business", "first"]),
                "language": "en",
                "dietary": random.sample(["vegetarian", "vegan", "halal", "kosher", "none"], k=1),
                "marketing_opt_in": random.random() > 0.5,
            },
            "saved_venues": random.sample(venue_ids, k=min(3, len(venue_ids))),
            "bookings": bookings,
            "payment_methods": [{
                "type": "card",
                "brand": random.choice(["visa", "mastercard", "amex"]),
                "last4": f"{random.randint(1000, 9999)}",
                "is_default": True,
            }],
            "created_at": datetime.now(timezone.utc) - timedelta(days=random.randint(30, 900)),
            "updated_at": datetime.now(timezone.utc) - timedelta(days=random.randint(0, 30)),
        })
    return users


def seed_database(venue_count: int, user_count: int = 300, reset: bool = False) -> None:
    venues_coll = get_venues_collection()
    users_coll = get_users_collection()

    if reset:
        print(f"Dropping existing collections: {venues_coll.name}, {users_coll.name}")
        venues_coll.drop()
        users_coll.drop()

    print(f"Generating {venue_count} venue documents...")
    venues = generate_venues(venue_count)
    print(f"Generating {user_count} user documents with embedded bookings...")
    users = generate_users(user_count, venues)

    if venues:
        venues_coll.insert_many(venues)
    if users:
        users_coll.insert_many(users)

    # Operational indexes that support the single-document reads in the demo
    # (NOT the Atlas Search autocomplete index -- that is created separately,
    # see search/atlas_search_index.json + README).
    venues_coll.create_index("venue_id", unique=True)
    venues_coll.create_index([("region.city", 1)])
    venues_coll.create_index([("category", 1)])
    users_coll.create_index("user_id", unique=True)

    print(f"Seeded {venues_coll.count_documents({})} venues and {users_coll.count_documents({})} users.")
    print("Next step: create the Atlas Search index from search/atlas_search_index.json")
    print("(Atlas UI -> Database -> your cluster -> Search -> Create Search Index -> JSON Editor).")


if __name__ == "__main__":
    from db.connection import SEED_DOCUMENT_COUNT

    parser = argparse.ArgumentParser(description="Seed AtlasTrips demo data into MongoDB Atlas.")
    parser.add_argument("--count", type=int, default=SEED_DOCUMENT_COUNT, help="Number of venue documents to generate.")
    parser.add_argument("--users", type=int, default=300, help="Number of user documents to generate.")
    parser.add_argument("--reset", action="store_true", help="Drop existing collections before seeding.")
    args = parser.parse_args()

    seed_database(venue_count=args.count, user_count=args.users, reset=args.reset)
