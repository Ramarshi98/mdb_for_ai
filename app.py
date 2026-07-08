"""
AtlasTrips -- app.py
====================
A Streamlit demo that doubles as the user-facing booking UI AND an
executive/technical demo of why a single MongoDB Atlas cluster (operational
data + Atlas Search) replaces a Postgres/Aurora + ORM + OpenSearch stack at
high scale.

Tab 1, "Travel Search": live type-ahead autocomplete (Atlas Search
$search/autocomplete) over hotels/lounges/events, a results pane, and a
Book Now flow that performs a real single-document update against Atlas.

Tab 2, "AI Concierge": a chatbot over persisted bookings and venues using
Voyage embeddings, Atlas Vector Search, and optional LLM generation.

Tab 3, "Behind the Scenes": the architecture diagram, the exact MQL that
just ran, and a performance scorecard that blends LIVE latency samples
(captured from this app + any running locustfile.py traffic, all written to
the same Atlas cluster) against a static reference baseline for the legacy
relational + search-silo stack.
"""

import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st
from st_keyup import st_keyup

from db.connection import (
    BOOKINGS_COLLECTION_NAME,
    COLLECTION_NAME,
    DB_NAME,
    MONGODB_ATLAS_URI,
    SEARCH_INDEX_NAME,
    get_bookings_collection,
    get_metrics_collection,
    get_venues_collection,
    ping,
)
from db.seed import seed_database

# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="AtlasTrips | MongoDB Atlas Demo",
    page_icon="✈️",
    layout="wide",
)

st.markdown(
    """
    <style>
    .venue-card {
        border: 1px solid rgba(120,120,120,0.25);
        border-radius: 12px;
        padding: 14px 16px;
        margin-bottom: 10px;
        background: rgba(120,120,120,0.04);
    }
    .venue-card h4 { margin: 0 0 4px 0; }
    .venue-meta { color: rgba(120,120,120,0.9); font-size: 0.85rem; margin-bottom: 6px; }
    .pill {
        display:inline-block; padding:2px 10px; border-radius:999px;
        background:rgba(46,160,67,0.15); font-size:0.75rem; margin-right:4px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

CATEGORY_ICON = {"hotel": "\U0001F3E8", "airport_lounge": "\U0001F6CB️", "event": "\U0001F3A4"}
CATEGORY_FILTER_MAP = {"Hotels": "hotel", "Lounges": "airport_lounge", "Events": "event"}
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
VOYAGE_BASE_URL = os.getenv("VOYAGE_BASE_URL", "https://ai.mongodb.com/v1")
VOYAGE_EMBED_MODEL = os.getenv("VOYAGE_EMBED_MODEL", "voyage-4-large")
BOOKING_VECTOR_INDEX_NAME = os.getenv("BOOKING_VECTOR_INDEX_NAME", "bookings_voyage_vector")
VENUE_VECTOR_INDEX_NAME = os.getenv("VENUE_VECTOR_INDEX_NAME", "venues_voyage_vector")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ---------------------------------------------------------------------------
# Guard: stop early with a friendly message if Atlas isn't configured yet.
# ---------------------------------------------------------------------------
if not MONGODB_ATLAS_URI:
    st.title("✈️ AtlasTrips")
    st.warning(
        "**MONGODB_ATLAS_URI is not set.** Copy `env.example` to `.env`, "
        "paste in your Atlas connection string, then restart the app.\n\n"
        "Also make sure you've run `python -m db.seed` and created the "
        "Atlas Search index from `search/atlas_search_index.json`."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
defaults = {
    "session_user_id": f"demo-user-{uuid.uuid4().hex[:8]}",
    "selected_venue_id": None,
    "my_bookings": [],
    "query_input": "",
    "chat_history": [],
    "pending_cancel_booking_id": None,
    "last_booking_context": [],
    "current_ai_trace": None,
    "last_ai_trace": None,
    "last_pipeline": None,
    "last_action_label": "No action yet -- run a search or open a venue to see its live MQL here.",
    "search_error": None,
}
for k, v in defaults.items():
    st.session_state.setdefault(k, v)


def select_suggestion(venue_id: str, venue_name: str) -> None:
    st.session_state.selected_venue_id = venue_id
    st.session_state.query_input = venue_name


def start_ai_trace(prompt: str) -> None:
    st.session_state.current_ai_trace = {
        "prompt": prompt,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
    }


def add_ai_trace_step(name: str, engine: str, detail: str, latency_ms: float) -> None:
    trace = st.session_state.get("current_ai_trace")
    if not trace:
        return
    trace["steps"].append({
        "step": len(trace["steps"]) + 1,
        "name": name,
        "engine": engine,
        "detail": detail,
        "latency_ms": round(latency_ms, 2),
    })


def finish_ai_trace(answer: str) -> None:
    trace = st.session_state.get("current_ai_trace")
    if not trace:
        return
    trace["answer_preview"] = answer[:500]
    trace["total_latency_ms"] = round(sum(step["latency_ms"] for step in trace["steps"]), 2)
    st.session_state.last_ai_trace = trace
    st.session_state.current_ai_trace = None


def _strip_mongo_id(doc: dict) -> dict:
    cleaned = dict(doc)
    if "_id" in cleaned:
        cleaned["_id"] = str(cleaned["_id"])
    return cleaned


def booking_to_text(booking: dict) -> str:
    return (
        f"Booking {booking.get('booking_id')} for {booking.get('venue_name')} in {booking.get('city')}. "
        f"Status: {booking.get('status', 'confirmed')}. Check-in: {booking.get('check_in')}. "
        f"Nights: {booking.get('nights')}. Total: {booking.get('total_price')} {booking.get('currency')}. "
        f"Venue id: {booking.get('venue_id')}."
    )


def venue_to_text(venue: dict) -> str:
    region = venue.get("region", {})
    pricing = venue.get("pricing", {})
    facilities = venue.get("facilities", {})
    amenities = ", ".join(facilities.get("amenities", []))
    return (
        f"{venue.get('name')} is a {venue.get('category')} in {region.get('city')}, "
        f"{region.get('country')} near {region.get('airport_code')}. "
        f"Description: {venue.get('description', '')}. Amenities: {amenities}. "
        f"Base rate: {pricing.get('base_rate')} {pricing.get('currency', 'USD')}. "
        f"Rating: {venue.get('rating')} from {venue.get('review_count')} reviews."
    )


def search_terms(text: str) -> list[str]:
    stopwords = {"about", "booking", "bookings", "venue", "venues", "what", "where", "when", "show", "tell", "please"}
    terms = []
    for term in re.findall(r"[A-Za-z0-9-]{3,}", text):
        lowered = term.lower()
        if lowered not in stopwords:
            terms.append(term)
    return terms[:5]


def embed_text(text: str, input_type: str = "document"):
    embeddings = embed_texts([text], input_type=input_type)
    return embeddings[0] if embeddings else None


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    texts = [text for text in texts if text.strip()]
    if not VOYAGE_API_KEY or not texts:
        add_ai_trace_step("Embedding skipped", "Voyage AI", "VOYAGE_API_KEY is not configured.", 0.0)
        return []
    start = time.perf_counter()
    try:
        response = requests.post(
            f"{VOYAGE_BASE_URL.rstrip('/')}/embeddings",
            headers={
                "Authorization": f"Bearer {VOYAGE_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "input": texts,
                "model": VOYAGE_EMBED_MODEL,
                "input_type": input_type,
                "truncation": True,
                "output_dtype": "float",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        embeddings = [item["embedding"] for item in sorted(payload.get("data", []), key=lambda item: item["index"])]
        add_ai_trace_step(
            "Create query embedding",
            "Voyage AI",
            f"POST `{VOYAGE_BASE_URL.rstrip('/')}/embeddings`, model `{VOYAGE_EMBED_MODEL}`, input_type `{input_type}`, {len(texts)} input(s).",
            (time.perf_counter() - start) * 1000,
        )
        return embeddings
    except Exception as exc:
        add_ai_trace_step(
            "Create query embedding",
            "Voyage AI",
            f"Embedding failed: {exc}",
            (time.perf_counter() - start) * 1000,
        )
        return []


def fetch_user_bookings(include_cancelled: bool = True) -> list[dict]:
    query = {"session_user_id": st.session_state.session_user_id}
    if not include_cancelled:
        query["status"] = "confirmed"
    start = time.perf_counter()
    try:
        docs = get_bookings_collection().find(query).sort("booked_at", -1).limit(50)
        results = [_strip_mongo_id(doc) for doc in docs]
        add_ai_trace_step(
            "Fetch user bookings",
            "MongoDB Atlas",
            f"find on `{BOOKINGS_COLLECTION_NAME}` returned {len(results)} booking(s).",
            (time.perf_counter() - start) * 1000,
        )
        return results
    except Exception as exc:
        add_ai_trace_step(
            "Fetch user bookings",
            "MongoDB Atlas",
            f"find failed, using session-state fallback: {exc}",
            (time.perf_counter() - start) * 1000,
        )
        return list(st.session_state.my_bookings)


def persist_booking(booking: dict) -> None:
    doc = dict(booking)
    doc["session_user_id"] = st.session_state.session_user_id
    doc["status"] = "confirmed" if booking.get("success") else "failed"
    doc["cancelled_at"] = None
    doc["embedding_text"] = booking_to_text(doc)
    embedding = embed_text(doc["embedding_text"], input_type="document")
    if embedding:
        doc["embedding"] = embedding
        doc["embedding_model"] = VOYAGE_EMBED_MODEL
    try:
        get_bookings_collection().insert_one(doc)
    except Exception:
        pass


def cancel_booking(booking_id: str) -> tuple[bool, str]:
    start = time.perf_counter()
    try:
        booking = get_bookings_collection().find_one({
            "booking_id": booking_id,
            "session_user_id": st.session_state.session_user_id,
        })
    except Exception as exc:
        add_ai_trace_step("Lookup booking to cancel", "MongoDB Atlas", f"find_one failed: {exc}", (time.perf_counter() - start) * 1000)
        return False, f"I could not look up booking `{booking_id}`: {exc}"
    add_ai_trace_step("Lookup booking to cancel", "MongoDB Atlas", f"find_one for `{booking_id}`.", (time.perf_counter() - start) * 1000)
    if not booking:
        return False, f"I could not find booking `{booking_id}` for this session."
    if booking.get("status") == "cancelled":
        return False, f"Booking `{booking_id}` is already cancelled."

    now = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    try:
        get_bookings_collection().update_one(
            {"booking_id": booking_id, "session_user_id": st.session_state.session_user_id},
            {"$set": {"status": "cancelled", "cancelled_at": now}},
        )
        get_venues_collection().update_one(
            {"venue_id": booking["venue_id"], "availability_calendar.date": booking["check_in"]},
            {"$inc": {"availability_calendar.$.available_units": 1}},
        )
    except Exception as exc:
        add_ai_trace_step("Cancel booking", "MongoDB Atlas", f"Booking/availability update failed: {exc}", (time.perf_counter() - start) * 1000)
        return False, f"I could not cancel booking `{booking_id}`: {exc}"
    add_ai_trace_step("Cancel booking", "MongoDB Atlas", "Updated booking status and restored one available unit.", (time.perf_counter() - start) * 1000)
    st.session_state.my_bookings = fetch_user_bookings()
    record_metric("booking_cancel", 0.0, True)
    return True, f"Cancelled booking `{booking_id}` for {booking.get('venue_name')}."


def search_booking_context(question: str, limit: int = 5) -> list[dict]:
    def fallback_booking_context(reason: str) -> list[dict]:
        terms = search_terms(question)
        bookings = fetch_user_bookings(include_cancelled=True)
        if not terms:
            results = bookings[:limit]
        else:
            filtered = [booking for booking in bookings if any(term.lower() in booking_to_text(booking).lower() for term in terms)]
            results = (filtered or bookings)[:limit]
        add_ai_trace_step(
            "Retrieve booking context",
            "MongoDB Atlas",
            f"{reason}; fallback booking lookup returned {len(results)} booking(s).",
            0.0,
        )
        return results

    embedding = embed_text(question, input_type="query")
    if embedding:
        pipeline = [
            {"$vectorSearch": {
                "index": BOOKING_VECTOR_INDEX_NAME,
                "path": "embedding",
                "queryVector": embedding,
                "numCandidates": 50,
                "limit": limit,
                "filter": {"session_user_id": st.session_state.session_user_id},
            }},
            {"$project": {"embedding": 0, "score": {"$meta": "vectorSearchScore"}}},
        ]
        try:
            st.session_state.last_pipeline = {"collection": BOOKINGS_COLLECTION_NAME, "operation": "aggregate", "pipeline": pipeline}
            st.session_state.last_action_label = f"Voyage embedding + Atlas Vector Search over bookings using `{VOYAGE_EMBED_MODEL}`."
            start = time.perf_counter()
            results = [_strip_mongo_id(doc) for doc in get_bookings_collection().aggregate(pipeline)]
            add_ai_trace_step(
                "Retrieve booking context",
                "MongoDB Atlas Vector Search",
                f"$vectorSearch on `{BOOKINGS_COLLECTION_NAME}` returned {len(results)} booking(s).",
                (time.perf_counter() - start) * 1000,
            )
            if results:
                return results
            return fallback_booking_context("Vector search returned no booking matches")
        except Exception as exc:
            add_ai_trace_step("Retrieve booking context", "MongoDB Atlas Vector Search", f"Vector search failed; falling back to find: {exc}", 0.0)
            return fallback_booking_context("Vector search failed")

    return fallback_booking_context("Embeddings unavailable")


def search_venue_context(question: str, limit: int = 5) -> list[dict]:
    embedding = embed_text(question, input_type="query")
    if embedding:
        pipeline = [
            {"$vectorSearch": {
                "index": VENUE_VECTOR_INDEX_NAME,
                "path": "embedding",
                "queryVector": embedding,
                "numCandidates": 75,
                "limit": limit,
            }},
            {"$project": {"embedding": 0, "score": {"$meta": "vectorSearchScore"}}},
        ]
        try:
            st.session_state.last_pipeline = {"collection": COLLECTION_NAME, "operation": "aggregate", "pipeline": pipeline}
            st.session_state.last_action_label = f"Voyage embedding + Atlas Vector Search over venues using `{VOYAGE_EMBED_MODEL}`."
            start = time.perf_counter()
            results = [_strip_mongo_id(doc) for doc in get_venues_collection().aggregate(pipeline)]
            add_ai_trace_step(
                "Retrieve venue context",
                "MongoDB Atlas Vector Search",
                f"$vectorSearch on `{COLLECTION_NAME}` returned {len(results)} venue(s).",
                (time.perf_counter() - start) * 1000,
            )
            return results
        except Exception as exc:
            add_ai_trace_step("Retrieve venue context", "MongoDB Atlas Vector Search", f"Vector search failed; falling back to find: {exc}", 0.0)
            pass

    terms = search_terms(question)
    if not terms:
        return []
    query = {"$or": []}
    for term in terms:
        pattern = re.escape(term)
        query["$or"].extend([
            {"name": {"$regex": pattern, "$options": "i"}},
            {"region.city": {"$regex": pattern, "$options": "i"}},
            {"region.country": {"$regex": pattern, "$options": "i"}},
            {"category": {"$regex": pattern, "$options": "i"}},
            {"description": {"$regex": pattern, "$options": "i"}},
        ])
    try:
        start = time.perf_counter()
        results = [_strip_mongo_id(doc) for doc in get_venues_collection().find(query, {"embedding": 0}).limit(limit)]
        add_ai_trace_step(
            "Retrieve venue context",
            "MongoDB Atlas",
            f"Fallback find on `{COLLECTION_NAME}` returned {len(results)} venue(s).",
            (time.perf_counter() - start) * 1000,
        )
        return results
    except Exception as exc:
        add_ai_trace_step("Retrieve venue context", "MongoDB Atlas", f"Fallback find failed: {exc}", 0.0)
        return []


def fetch_venues_for_bookings(bookings: list[dict]) -> list[dict]:
    venue_ids = sorted({booking.get("venue_id") for booking in bookings if booking.get("venue_id")})
    if not venue_ids:
        return []
    start = time.perf_counter()
    try:
        docs = get_venues_collection().find(
            {"venue_id": {"$in": venue_ids}},
            {"embedding": 0},
        )
        results = [_strip_mongo_id(doc) for doc in docs]
        add_ai_trace_step(
            "Enrich booked venues",
            "MongoDB Atlas",
            f"Exact indexed lookup by `venue_id` returned {len(results)} venue(s) for {len(venue_ids)} booking venue id(s).",
            (time.perf_counter() - start) * 1000,
        )
        return results
    except Exception as exc:
        add_ai_trace_step(
            "Enrich booked venues",
            "MongoDB Atlas",
            f"Exact booked-venue lookup failed: {exc}",
            (time.perf_counter() - start) * 1000,
        )
        return []


def merge_venues(*venue_groups: list[dict]) -> list[dict]:
    merged = {}
    for venues in venue_groups:
        for venue in venues:
            key = venue.get("venue_id") or venue.get("_id")
            if key and key not in merged:
                merged[key] = venue
    return list(merged.values())


def backfill_venue_embeddings(limit: int = 200) -> tuple[int, str | None]:
    if not VOYAGE_API_KEY:
        return 0, "VOYAGE_API_KEY is not set."
    try:
        venues = list(get_venues_collection().find(
            {"embedding": {"$exists": False}},
            {"embedding": 0},
        ).limit(limit))
        if not venues:
            return 0, None
        updated = 0
        for i in range(0, len(venues), 16):
            batch = venues[i:i + 16]
            texts = [venue_to_text(venue) for venue in batch]
            embeddings = embed_texts(texts, input_type="document")
            for venue, embedding, text in zip(batch, embeddings, texts):
                get_venues_collection().update_one(
                    {"_id": venue["_id"]},
                    {"$set": {"embedding": embedding, "embedding_model": VOYAGE_EMBED_MODEL, "embedding_text": text}},
                )
                updated += 1
        return updated, None
    except Exception as exc:
        return 0, str(exc)


def summarize_bookings(bookings: list[dict]) -> str:
    if not bookings:
        return "You do not have any bookings in this session yet."
    lines = ["Here are your bookings:"]
    for booking in bookings:
        price = booking.get("total_price")
        price_text = f"${price:,.2f}" if isinstance(price, (int, float)) else "--"
        lines.append(
            f"- `{booking.get('booking_id')}`: {booking.get('venue_name')} in {booking.get('city')}, "
            f"check-in {booking.get('check_in')} for {booking.get('nights')} night(s), "
            f"{booking.get('status', 'confirmed')}, {price_text} {booking.get('currency', 'USD')}"
        )
    return "\n".join(lines)


def resolve_booking_from_context(prompt: str, active_bookings: list[dict]) -> tuple[dict | None, str | None]:
    if not active_bookings:
        return None, "You do not have any active bookings to cancel."

    terms = [
        term for term in search_terms(prompt)
        if term.lower() not in {"cancel", "cancelled", "cancellation", "that", "this", "one", "it", "active"}
    ]

    if terms:
        scored = []
        for booking in active_bookings:
            haystack = booking_to_text(booking).lower()
            score = sum(1 for term in terms if term.lower() in haystack)
            if score:
                scored.append((score, booking))
        if scored:
            scored.sort(key=lambda item: item[0], reverse=True)
            best_score = scored[0][0]
            matches = [booking for score, booking in scored if score == best_score]
            if len(matches) == 1:
                return matches[0], None
            return None, summarize_bookings(matches) + "\n\nI found multiple matching bookings. Which one should I cancel?"

    contextual = [
        booking for booking in st.session_state.last_booking_context
        if booking.get("status", "confirmed") == "confirmed"
    ]
    if len(contextual) == 1:
        return contextual[0], None

    if len(active_bookings) == 1:
        return active_bookings[0], None

    return None, summarize_bookings(active_bookings) + "\n\nWhich booking should I cancel? You can describe the city, venue, date, or paste the booking ID."


def fallback_chat_answer(question: str, bookings: list[dict], venues: list[dict]) -> str:
    parts = []
    if bookings:
        parts.append(summarize_bookings(bookings))
    if venues:
        venue_lines = ["Relevant venues I found:"]
        for venue in venues:
            region = venue.get("region", {})
            venue_lines.append(f"- {venue.get('name')} in {region.get('city')}, {region.get('country')} ({venue.get('category')})")
        parts.append("\n".join(venue_lines))
    if parts:
        return "\n\n".join(parts)
    return "I could not find matching bookings or venues yet. Try asking about a booking ID, city, or venue name."


def extract_responses_text(payload: dict) -> str:
    if payload.get("output_text"):
        return payload["output_text"]
    chunks = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def generate_chat_answer(question: str, bookings: list[dict], venues: list[dict]) -> str:
    if not OPENAI_API_KEY:
        return fallback_chat_answer(question, bookings, venues)
    try:
        context = {
            "bookings": [{k: v for k, v in b.items() if k != "embedding"} for b in bookings],
            "venues": [{k: v for k, v in vdoc.items() if k != "embedding"} for vdoc in venues],
        }
        base_url = (OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
        start = time.perf_counter()
        response = requests.post(
            f"{base_url}/responses",
            headers={
                "Content-Type": "application/json",
                "api-key": OPENAI_API_KEY,
                "Authorization": f"Bearer {OPENAI_API_KEY}",
            },
            json={
                "model": OPENAI_MODEL,
                "input": (
                    "You are AtlasTrips AI Concierge. Answer only from the provided MongoDB context. "
                    "Be concise and practical. If context is missing, say what information is missing. "
                    "Do not cancel bookings; cancellation is handled by the app confirmation flow.\n\n"
                    f"Question: {question}\n\nMongoDB context:\n{json.dumps(context, default=str)[:12000]}"
                ),
            },
            timeout=45,
        )
        response.raise_for_status()
        answer = extract_responses_text(response.json())
        add_ai_trace_step(
            "Generate answer",
            "LLM Gateway",
            f"Responses API call to model `{OPENAI_MODEL}`.",
            (time.perf_counter() - start) * 1000,
        )
        return answer or fallback_chat_answer(question, bookings, venues)
    except Exception as exc:
        add_ai_trace_step("Generate answer", "LLM Gateway", f"Responses API call failed: {exc}", (time.perf_counter() - start) * 1000 if "start" in locals() else 0.0)
        return (
            f"I found relevant MongoDB context, but the OpenAI-compatible chat call failed: {exc}. "
            "Check `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, and network access.\n\n"
            + fallback_chat_answer(question, bookings, venues)
        )


def handle_chat(prompt: str) -> str:
    text = prompt.strip()
    start_ai_trace(text)
    route_start = time.perf_counter()
    lower = text.lower()
    booking_ids = re.findall(r"BKG-[\w-]+", text, flags=re.IGNORECASE)

    def respond(answer: str) -> str:
        finish_ai_trace(answer)
        return answer

    add_ai_trace_step("Route request", "Streamlit App", "Classified the prompt and selected the execution path.", (time.perf_counter() - route_start) * 1000)

    if st.session_state.pending_cancel_booking_id and lower in {"yes", "y", "confirm", "confirm cancel", "cancel it"}:
        booking_id = st.session_state.pending_cancel_booking_id
        st.session_state.pending_cancel_booking_id = None
        _, message = cancel_booking(booking_id)
        return respond(message)

    if "cancel" in lower:
        active = fetch_user_bookings(include_cancelled=False)
        if booking_ids:
            booking_id = booking_ids[0].upper()
            st.session_state.pending_cancel_booking_id = booking_id
            return respond(f"Please confirm: should I cancel booking `{booking_id}`? Reply `confirm` to proceed.")
        booking, clarification = resolve_booking_from_context(text, active)
        if booking:
            booking_id = booking["booking_id"]
            st.session_state.pending_cancel_booking_id = booking_id
            return respond(
                f"I found `{booking_id}` for {booking.get('venue_name')} in {booking.get('city')}, "
                f"check-in {booking.get('check_in')}. Reply `confirm` to cancel it."
            )
        return respond(clarification or "Which booking should I cancel?")

    if "show bookings" in lower:
        bookings = fetch_user_bookings()
        st.session_state.last_booking_context = bookings
        return respond(summarize_bookings(bookings))

    bookings = search_booking_context(text)
    st.session_state.last_booking_context = bookings
    booked_venues = fetch_venues_for_bookings(bookings)
    venues = merge_venues(booked_venues, search_venue_context(text))
    return respond(generate_chat_answer(text, bookings, venues))

# ---------------------------------------------------------------------------
# Metrics: every operation below is timed and written to METRICS_COLLECTION
# so the Performance Scorecard tab can show genuinely live numbers, not just
# a static slide.
# ---------------------------------------------------------------------------
def record_metric(operation: str, latency_ms: float, success: bool) -> None:
    try:
        get_metrics_collection().insert_one({
            "ts": datetime.now(timezone.utc),
            "operation": operation,
            "latency_ms": latency_ms,
            "success": success,
            "engine": "mongodb_atlas",
            "source": "streamlit_app",
        })
    except Exception:
        pass  # metrics are best-effort; never block the user-facing flow


def _percentile(values, pct: float):
    if not values:
        return None
    data = sorted(values)
    k = (len(data) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(data) - 1)
    if f == c:
        return data[f]
    return data[f] * (c - k) + data[c] * (k - f)


def fetch_live_mongo_stats(window_minutes: int = 5):
    """Aggregate real latency/outcome samples written by THIS app and by any
    concurrently running `locustfile.py` load test, all stored in the same
    Atlas cluster (unified workload telemetry, no separate metrics stack)."""
    try:
        coll = get_metrics_collection()
        since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        docs = list(coll.find({"ts": {"$gte": since}}, {"latency_ms": 1, "success": 1}).limit(50000))
    except Exception:
        return None
    if not docs:
        return None
    latencies = [d["latency_ms"] for d in docs if "latency_ms" in d]
    successes = sum(1 for d in docs if d.get("success"))
    return {
        "count": len(docs),
        "p50": _percentile(latencies, 50),
        "p95": _percentile(latencies, 95),
        "p99": _percentile(latencies, 99),
        "error_rate_pct": 100.0 * (1 - successes / len(docs)),
        "approx_rps": len(docs) / (window_minutes * 60),
    }


# Customer baseline placeholder for the legacy stack. Fill these in during
# discovery for an apples-to-apples comparison.
LEGACY_REFERENCE_STATS = {
    "label": "Aurora (Postgres) + ORM + OpenSearch sync — customer baseline",
    "p50": None, "p95": None, "p99": None,
    "error_rate_pct": None, "approx_rps": None,
    "note": "Ask the customer for their Aurora + ORM + OpenSearch baseline metrics, "
            "then fill them in here for an apples-to-apples comparison.",
}
MONGO_REFERENCE_STATS = {
    "label": "MongoDB Atlas ($search + operational, unified) — reference baseline",
    "p50": 0, "p95": 0, "p99": 0,
    "error_rate_pct": 0.0, "approx_rps": 0,
    "note": "Illustrative reference figures shown until live samples are available below.",
}

# ---------------------------------------------------------------------------
# Data access -- every function times itself and records the exact MQL it
# ran into session_state so the "Behind the Scenes" tab can show it verbatim.
# ---------------------------------------------------------------------------
def autocomplete_search(query: str, category_label: str = "All", limit: int = 8):
    coll = get_venues_collection()
    compound = {
        "should": [
            {"autocomplete": {"query": query, "path": "name", "fuzzy": {"maxEdits": 1, "maxExpansions": 50}}},
            {"autocomplete": {"query": query, "path": "region.city"}},
            {"autocomplete": {"query": query, "path": "category"}},
        ],
        "minimumShouldMatch": 1,
    }
    if category_label != "All":
        compound["filter"] = [{"text": {"query": CATEGORY_FILTER_MAP[category_label], "path": "category"}}]

    pipeline = [
        {"$search": {"index": SEARCH_INDEX_NAME, "compound": compound}},
        {"$limit": limit},
        {"$project": {
            "_id": 0, "venue_id": 1, "type": 1, "category": 1, "name": 1, "brand": 1,
            "region": 1, "rating": 1, "review_count": 1, "price_tier": 1,
            "pricing": 1, "score": {"$meta": "searchScore"},
        }},
    ]

    start = time.perf_counter()
    success, results = True, []
    try:
        results = list(coll.aggregate(pipeline))
        st.session_state.search_error = None
    except Exception as exc:
        success = False
        st.session_state.search_error = str(exc)
    latency_ms = (time.perf_counter() - start) * 1000
    record_metric("autocomplete_search", latency_ms, success)

    st.session_state.last_pipeline = {"collection": COLLECTION_NAME, "operation": "aggregate", "pipeline": pipeline}
    st.session_state.last_action_label = f'Atlas Search autocomplete for "{query}" ({latency_ms:.1f} ms, {len(results)} hits)'
    return results, latency_ms


def trending_venues(n: int = 6):
    try:
        return list(get_venues_collection().aggregate([{"$sample": {"size": n}}]))
    except Exception:
        return []


def get_venue_by_id(venue_id: str):
    coll = get_venues_collection()
    query = {"venue_id": venue_id}
    start = time.perf_counter()
    success, doc = True, None
    try:
        doc = coll.find_one(query)
    except Exception:
        success = False
    latency_ms = (time.perf_counter() - start) * 1000
    record_metric("single_doc_read", latency_ms, success)

    st.session_state.last_pipeline = {"collection": COLLECTION_NAME, "operation": "find_one", "query": query}
    st.session_state.last_action_label = f"Single-document read for {venue_id} ({latency_ms:.1f} ms) -- everything the UI needs in one round trip, no JOINs."
    return doc, latency_ms


def book_venue(venue: dict, date_str: str, nights: int):
    coll = get_venues_collection()
    update_filter = {"venue_id": venue["venue_id"], "availability_calendar.date": date_str}
    update_op = {"$inc": {"availability_calendar.$.available_units": -1}}

    start = time.perf_counter()
    success = True
    try:
        result = coll.update_one(update_filter, update_op)
        success = result.matched_count > 0
    except Exception:
        success = False
    latency_ms = (time.perf_counter() - start) * 1000
    record_metric("booking_write", latency_ms, success)

    st.session_state.last_pipeline = {"collection": COLLECTION_NAME, "operation": "update_one", "filter": update_filter, "update": update_op}
    st.session_state.last_action_label = f"Booking write for {venue['venue_id']} ({latency_ms:.1f} ms) -- atomic positional $ update on the single venue document."

    booking = {
        "booking_id": f"BKG-{int(time.time())}-{random.randint(100, 999)}",
        "venue_id": venue["venue_id"],
        "venue_name": venue["name"],
        "city": venue["region"]["city"],
        "check_in": date_str,
        "nights": nights,
        "total_price": round(venue["pricing"]["base_rate"] * nights, 2),
        "currency": venue["pricing"].get("currency", "USD"),
        "booked_at": datetime.now(timezone.utc).isoformat(),
        "success": success,
    }
    persist_booking(booking)
    return booking, latency_ms


# ---------------------------------------------------------------------------
# Sidebar: cluster status + one-click seeding
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Cluster status")
    online = ping()
    status_label = "\U0001F7E2 Connected" if online else "\U0001F534 Unreachable"
    st.markdown(f"{status_label} -- `{DB_NAME}.{COLLECTION_NAME}`")
    if online:
        try:
            st.caption(f"{get_venues_collection().estimated_document_count():,} venue documents")
        except Exception:
            pass
    st.divider()
    with st.expander("⚙️ Seed / reset demo data"):
        seed_count = st.number_input("Venues to generate", min_value=100, max_value=50000, value=1000, step=100)
        reset_first = st.checkbox("Drop existing collections first", value=False)
        if st.button("Run seeder", use_container_width=True):
            with st.spinner("Seeding MongoDB Atlas..."):
                seed_database(venue_count=int(seed_count), user_count=max(50, int(seed_count) // 10), reset=reset_first)
            st.success("Seed complete.")
            st.rerun()
    with st.expander("🧠 Voyage embeddings"):
        st.caption(f"Model: `{VOYAGE_EMBED_MODEL}`")
        embed_count = st.number_input("Venues to embed", min_value=10, max_value=2000, value=200, step=10)
        if st.button("Backfill venue embeddings", use_container_width=True, disabled=not VOYAGE_API_KEY):
            with st.spinner("Embedding venues with Voyage AI..."):
                updated, error = backfill_venue_embeddings(limit=int(embed_count))
            if error:
                st.error(error)
            else:
                st.success(f"Embedded {updated:,} venue document(s).")
        if not VOYAGE_API_KEY:
            st.caption("Set VOYAGE_API_KEY to enable embedding backfill.")
    st.divider()
    st.caption(
        "Run a load test against this same cluster:\n\n"
        "`locust -f locustfile.py`\n\nthen open http://localhost:8089"
    )

# ---------------------------------------------------------------------------
# Header + tabs
# ---------------------------------------------------------------------------
st.title("✈️ AtlasTrips")
st.caption("One MongoDB Atlas cluster. Operational reads, Atlas Search autocomplete, and booking writes -- no OpenSearch, no ORM, no JOINs.")

tab_ui, tab_chat, tab_internals = st.tabs(["\U0001F9ED Travel Search", "\U0001F916 AI Concierge", "\U0001F6E0️ Behind the Scenes"])

# ===========================================================================
# TAB 1 -- Travel Search (user-facing UI)
# ===========================================================================
with tab_ui:
    filter_col, _ = st.columns([2, 3])
    with filter_col:
        category_label = st.radio("Category", options=["All", "Hotels", "Lounges", "Events"], horizontal=True)
    category_label = category_label or "All"

    query = st_keyup(
        "Search",
        value=st.session_state.query_input,
        placeholder="Try ‘Marriott’, ‘Singapore’, ‘lounge’, ‘LHR’...",
        label_visibility="collapsed",
        key="query_input",
        debounce=150,
    )
    query = query or ""

    results, suggestions_latency_ms = ([], None)
    if query and len(query.strip()) >= 1:
        results, suggestions_latency_ms = autocomplete_search(query.strip(), category_label, limit=8)
        if st.session_state.search_error:
            st.error(
                "Atlas Search query failed -- has the `venues_autocomplete` index been created yet? "
                f"({st.session_state.search_error})"
            )
        elif results:
            with st.container(border=True):
                st.caption(f"\U0001F50D {len(results)} suggestions in {suggestions_latency_ms:.1f} ms")
                for r in results:
                    icon = CATEGORY_ICON.get(r.get("category"), "\U0001F4CD")
                    label = f"{icon} {r['name']} — {r['region']['city']}, {r['region']['country']}"
                    st.button(
                        label,
                        key=f"sugg_{r['venue_id']}",
                        use_container_width=True,
                        on_click=select_suggestion,
                        args=(r["venue_id"], r["name"]),
                    )
        else:
            st.info("No matches yet -- keep typing, or try a city/airport code like ‘DXB’.")

    st.markdown("#### " + ("Search results" if query else "Trending now"))
    browse_set = results if query else trending_venues(6)
    cols = st.columns(3)
    for i, v in enumerate(browse_set):
        with cols[i % 3]:
            icon = CATEGORY_ICON.get(v.get("category"), "\U0001F4CD")
            price = v.get("pricing", {}).get("base_rate")
            price_str = f"${price:,.0f}" if price is not None else "--"
            st.markdown(
                f"""
                <div class="venue-card">
                    <h4>{icon} {v['name']}</h4>
                    <div class="venue-meta">{v['region']['city']}, {v['region']['country']} &middot; {v.get('region', {}).get('airport_code', '')}</div>
                    <span class="pill">⭐ {v.get('rating', '?')}</span>
                    <span class="pill">{v.get('price_tier', '')} {price_str}/night</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button("View / Book", key=f"view_{v['venue_id']}_{i}", use_container_width=True):
                st.session_state.selected_venue_id = v["venue_id"]
                st.rerun()

    # ---- Selected venue detail + Book Now ----
    if st.session_state.selected_venue_id:
        st.divider()
        venue, read_latency_ms = get_venue_by_id(st.session_state.selected_venue_id)
        if not venue:
            st.warning("That venue is no longer available.")
        else:
            icon = CATEGORY_ICON.get(venue.get("category"), "\U0001F4CD")
            st.subheader(f"{icon} {venue['name']}")
            st.caption(f"Loaded with a single find_one() in {read_latency_ms:.1f} ms")
            d1, d2 = st.columns([2, 1])
            with d1:
                st.write(venue.get("description", ""))
                fac = venue.get("facilities", {})
                amenity_text = ", ".join(fac.get("amenities", [])) or "--"
                st.markdown(f"**Amenities:** {amenity_text}")
                avail = venue.get("availability_calendar", [])[:7]
                if avail:
                    st.markdown("**Next 7 days availability**")
                    st.dataframe(pd.DataFrame(avail), hide_index=True, use_container_width=True)
            with d2:
                st.metric("Rating", f"⭐ {venue.get('rating', '?')}", f"{venue.get('review_count', 0):,} reviews")
                st.metric("Rate", f"${venue['pricing']['base_rate']:,.2f}", venue.get("price_tier", ""))

            bookable_dates = [a["date"] for a in venue.get("availability_calendar", []) if a.get("available_units", 0) > 0]
            with st.form(key=f"book_form_{venue['venue_id']}"):
                st.markdown("##### Book Now")
                if not bookable_dates:
                    st.warning("Sold out for the visible window.")
                bc1, bc2, bc3 = st.columns(3)
                with bc1:
                    chosen_date = st.selectbox("Check-in", bookable_dates) if bookable_dates else None
                with bc2:
                    nights = st.number_input("Nights", min_value=1, max_value=14, value=2)
                with bc3:
                    st.write("")
                    submitted = st.form_submit_button("Confirm booking", use_container_width=True, disabled=not bookable_dates)
                if submitted and chosen_date:
                    booking, write_latency_ms = book_venue(venue, chosen_date, int(nights))
                    st.session_state.my_bookings.append(booking)
                    st.success(
                        f"Booked! **{booking['booking_id']}** — {booking['nights']} night(s) at "
                        f"{booking['venue_name']} from {booking['check_in']} — "
                        f"${booking['total_price']:,.2f} {booking['currency']} "
                        f"(write committed in {write_latency_ms:.1f} ms)"
                    )
                    st.rerun()

    persisted_bookings = fetch_user_bookings()
    if persisted_bookings:
        with st.expander(f"\U0001F9F3 This session's bookings ({len(persisted_bookings)})"):
            st.dataframe(pd.DataFrame(persisted_bookings), hide_index=True, use_container_width=True)

# ===========================================================================
# TAB 2 -- AI Concierge
# ===========================================================================
with tab_chat:
    st.markdown(
        """
        <style>
        .ai-concierge-header {
            border: 1px solid rgba(120,120,120,0.24);
            border-radius: 18px;
            padding: 18px 20px;
            margin-bottom: 14px;
            background: linear-gradient(135deg, rgba(19,170,82,0.12), rgba(90,108,255,0.10));
        }
        .ai-concierge-header h3 { margin: 0 0 6px 0; }
        .ai-concierge-header p { margin: 0; color: rgba(120,120,120,0.95); }
        .ai-latest-label {
            margin: 16px 0 8px;
            color: rgba(120,120,120,0.95);
            font-size: 0.9rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .ai-composer-help {
            margin: 12px 0 6px;
            color: rgba(120,120,120,0.95);
            font-size: 0.9rem;
        }
        .ai-empty-state {
            border: 1px dashed rgba(120,120,120,0.35);
            border-radius: 16px;
            padding: 18px;
            margin-top: 14px;
            color: rgba(120,120,120,0.95);
            background: rgba(120,120,120,0.035);
        }
        </style>
        <div class="ai-concierge-header">
            <h3>AI Concierge</h3>
            <p>Ask about bookings, cancellations, venues, cities, dates, or amenities. Voyage embeddings retrieve MongoDB context; the chat model turns it into an answer.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    setup_notes = []
    if not VOYAGE_API_KEY:
        setup_notes.append("`VOYAGE_API_KEY` is not set, so semantic retrieval falls back to regular MongoDB lookups.")
    if not OPENAI_API_KEY:
        setup_notes.append("`OPENAI_API_KEY` is not set, so answers use deterministic summaries instead of LLM generation.")
    if setup_notes:
        st.info(" ".join(setup_notes))

    c1, c2, c3 = st.columns(3)
    c1.metric("Session user", st.session_state.session_user_id)
    c2.metric("Bookings", len(fetch_user_bookings()))
    c3.metric("Chat model", OPENAI_MODEL if OPENAI_API_KEY else "Not configured")

    st.markdown('<div class="ai-composer-help">Ask a question or request an action</div>', unsafe_allow_html=True)
    with st.form("ai_concierge_form", clear_on_submit=True, border=False):
        prompt_col, send_col = st.columns([7, 1])
        with prompt_col:
            prompt = st.text_input(
                "Message",
                placeholder="Try: Cancel my Singapore booking, show bookings, or tell me about the hotel I booked",
                label_visibility="collapsed",
            )
        with send_col:
            submitted = st.form_submit_button("Send", use_container_width=True)

    if submitted and prompt.strip():
        prompt = prompt.strip()
        st.session_state.chat_history.append({"role": "user", "content": prompt})
        with st.spinner("Retrieving context from MongoDB..."):
            answer = handle_chat(prompt)
        st.session_state.chat_history.append({"role": "assistant", "content": answer})

    if st.session_state.chat_history:
        st.markdown('<div class="ai-latest-label">Latest conversation</div>', unsafe_allow_html=True)
        for message in reversed(st.session_state.chat_history):
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
    else:
        st.markdown(
            """
            <div class="ai-empty-state">
                Start with something like <strong>show bookings</strong>,
                <strong>cancel my Singapore booking</strong>, or
                <strong>what amenities does my hotel have?</strong>
            </div>
            """,
            unsafe_allow_html=True,
        )

# ===========================================================================
# TAB 3 -- Behind the Scenes
# ===========================================================================
with tab_internals:
    sub_arch, sub_query, sub_ai_trace, sub_perf = st.tabs([
        "\U0001F5FA️ Architecture",
        "\U0001F50E MQL Query Inspector",
        "\U0001F916 AI Concierge Trace",
        "\U0001F4CA Performance Scorecard",
    ])

    # ---- Architecture diagram ----
    with sub_arch:
        st.markdown(
            "**Why one engine wins.** AtlasTrips' `venues` collection embeds region, "
            "pricing, availability, and facilities directly on each document. A single "
            "`$search` or `find_one()` call returns everything the UI needs. The "
            "legacy alternative spreads that same data across normalized SQL tables "
            "(JOINed on every read) and a separately-synced OpenSearch cluster for "
            "search -- two systems to keep consistent, scale, and pay for."
        )
        st.markdown(
            """
            <style>
            .arch-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
                margin-top: 18px;
            }
            .arch-lane {
                border: 1px solid rgba(120,120,120,0.25);
                border-radius: 16px;
                padding: 18px 18px 20px;
                background: rgba(120,120,120,0.04);
            }
            .arch-lane h4 { margin: 0; }
            .arch-summary {
                margin: 6px 0 16px;
                color: rgba(120,120,120,0.95);
                font-size: 0.92rem;
                line-height: 1.35;
            }
            .arch-step {
                display: grid;
                grid-template-columns: 34px 1fr;
                gap: 12px;
                align-items: start;
                border-radius: 12px;
                padding: 12px;
                margin: 8px 0;
                line-height: 1.25;
                box-shadow: 0 1px 8px rgba(0,0,0,0.06);
            }
            .arch-num {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 30px;
                height: 30px;
                border-radius: 999px;
                color: white;
                font-weight: 800;
                font-size: 0.88rem;
            }
            .arch-title { font-weight: 750; }
            .arch-detail { display: block; font-weight: 400; font-size: 0.88rem; margin-top: 4px; }
            .arch-neutral { background: #eef1f5; border: 1px solid #8a93a3; color: #2b3340; }
            .arch-mongo { background: #e7f7ee; border: 2px solid #13aa52; color: #0a3d22; }
            .arch-legacy { background: #fdeceb; border: 2px solid #c0392b; color: #5a1f18; }
            .arch-neutral .arch-num { background: #64748b; }
            .arch-mongo .arch-num { background: #13aa52; }
            .arch-legacy .arch-num { background: #c0392b; }
            .arch-arrow {
                text-align: center;
                color: rgba(120,120,120,0.9);
                font-weight: 800;
                letter-spacing: 0.03em;
            }
            .arch-split {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 12px;
                margin: 8px 0;
            }
            .arch-branch-label {
                text-align: center;
                font-size: 0.78rem;
                font-weight: 800;
                color: rgba(120,120,120,0.95);
                margin-bottom: 6px;
                text-transform: uppercase;
            }
            @media (max-width: 900px) {
                .arch-grid, .arch-split { grid-template-columns: 1fr; }
            }
            </style>
            <div class="arch-grid">
                <div class="arch-lane">
                    <h4>MongoDB Atlas path</h4>
                    <div class="arch-summary">One request, one database engine, one enriched document back to the app.</div>
                    <div class="arch-step arch-neutral">
                        <div class="arch-num">1</div>
                        <div><div class="arch-title">User types or opens a venue</div><span class="arch-detail">The UI needs search results plus venue details.</span></div>
                    </div>
                    <div class="arch-arrow">then</div>
                    <div class="arch-step arch-neutral">
                        <div class="arch-num">2</div>
                        <div><div class="arch-title">Web / App Server sends one query</div><span class="arch-detail">Autocomplete uses `$search`; venue detail uses `find_one()`.</span></div>
                    </div>
                    <div class="arch-arrow">then</div>
                    <div class="arch-step arch-mongo">
                        <div class="arch-num">3</div>
                        <div><div class="arch-title">MongoDB Atlas handles search and data</div><span class="arch-detail">Atlas Search and operational reads run against the same `venues` collection.</span></div>
                    </div>
                    <div class="arch-arrow">then</div>
                    <div class="arch-step arch-mongo">
                        <div class="arch-num">4</div>
                        <div><div class="arch-title">Complete document returns</div><span class="arch-detail">Region, pricing, availability, and facilities are already embedded.</span></div>
                    </div>
                </div>
                <div class="arch-lane">
                    <h4>Aurora + ORM + OpenSearch path</h4>
                    <div class="arch-summary">The app must coordinate normalized tables, search infrastructure, and merge logic.</div>
                    <div class="arch-step arch-neutral">
                        <div class="arch-num">1</div>
                        <div><div class="arch-title">User types or opens a venue</div><span class="arch-detail">The UI still needs search results plus venue details.</span></div>
                    </div>
                    <div class="arch-arrow">then</div>
                    <div class="arch-step arch-neutral">
                        <div class="arch-num">2</div>
                        <div><div class="arch-title">Web / App Server fans out</div><span class="arch-detail">Search and source-of-truth data live in separate systems.</span></div>
                    </div>
                    <div class="arch-arrow">parallel paths</div>
                    <div class="arch-split">
                        <div>
                            <div class="arch-branch-label">Relational read path</div>
                            <div class="arch-step arch-legacy">
                                <div class="arch-num">3A</div>
                                <div><div class="arch-title">ORM builds SQL</div><span class="arch-detail">The app maps objects to normalized tables.</span></div>
                            </div>
                            <div class="arch-step arch-legacy">
                                <div class="arch-num">4A</div>
                                <div><div class="arch-title">Aurora joins tables</div><span class="arch-detail">Venues, regions, pricing, availability, and facilities are joined together.</span></div>
                            </div>
                        </div>
                        <div>
                            <div class="arch-branch-label">Search index path</div>
                            <div class="arch-step arch-legacy">
                                <div class="arch-num">3B</div>
                                <div><div class="arch-title">CDC sync feeds OpenSearch</div><span class="arch-detail">Changes must be copied from Aurora into a second system.</span></div>
                            </div>
                            <div class="arch-step arch-legacy">
                                <div class="arch-num">4B</div>
                                <div><div class="arch-title">OpenSearch returns hits</div><span class="arch-detail">Results can lag behind the source database.</span></div>
                            </div>
                        </div>
                    </div>
                    <div class="arch-arrow">then</div>
                    <div class="arch-step arch-legacy">
                        <div class="arch-num">5</div>
                        <div><div class="arch-title">App reconciles both responses</div><span class="arch-detail">The app merges search hits with joined SQL data.</span></div>
                    </div>
                    <div class="arch-arrow">then</div>
                    <div class="arch-step arch-legacy">
                        <div class="arch-num">6</div>
                        <div><div class="arch-title">Response returns to the user</div><span class="arch-detail">More hops, more moving parts, and more failure modes.</span></div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ---- MQL inspector ----
    with sub_query:
        st.markdown("Shows the **exact** PyMongo call most recently triggered by the Travel Search tab.")
        st.info(st.session_state.last_action_label)
        if st.session_state.last_pipeline:
            st.code(json.dumps(st.session_state.last_pipeline, indent=2, default=str), language="json")
        else:
            st.code(
                json.dumps({
                    "collection": COLLECTION_NAME,
                    "operation": "aggregate",
                    "pipeline": [
                        {"$search": {"index": SEARCH_INDEX_NAME, "compound": {
                            "should": [
                                {"autocomplete": {"query": "<your input>", "path": "name"}},
                                {"autocomplete": {"query": "<your input>", "path": "region.city"}},
                                {"autocomplete": {"query": "<your input>", "path": "category"}},
                            ]
                        }}},
                        {"$limit": 8},
                    ],
                }, indent=2),
                language="json",
            )

    # ---- AI Concierge trace ----
    with sub_ai_trace:
        st.markdown(
            "Shows the **last AI Concierge request path** from prompt routing through Voyage embeddings, "
            "Atlas retrieval, and LLM generation. The goal is to make it clear where latency is spent."
        )
        trace = st.session_state.last_ai_trace
        if not trace:
            st.info("Ask the AI Concierge a question first, then come back here to inspect the trace.")
        else:
            st.caption(f"Prompt: `{trace['prompt']}`")
            steps = trace.get("steps", [])
            total_ms = trace.get("total_latency_ms", 0.0)
            by_engine = {}
            for step in steps:
                by_engine[step["engine"]] = by_engine.get(step["engine"], 0.0) + step["latency_ms"]
            llm_ms = by_engine.get("LLM Gateway", 0.0)
            atlas_ms = sum(ms for engine, ms in by_engine.items() if "MongoDB" in engine)
            voyage_ms = by_engine.get("Voyage AI", 0.0)

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total traced latency", f"{total_ms:,.1f} ms")
            m2.metric("LLM", f"{llm_ms:,.1f} ms")
            m3.metric("Voyage embeddings", f"{voyage_ms:,.1f} ms")
            m4.metric("Atlas retrieval/writes", f"{atlas_ms:,.1f} ms")

            if total_ms and llm_ms:
                st.success(f"LLM generation accounted for about {(llm_ms / total_ms) * 100:.1f}% of traced latency on this request.")
            elif total_ms:
                st.info("This request did not call the LLM path, so latency is mostly routing and MongoDB operations.")

            st.markdown(
                """
                <style>
                .ai-trace-flow {
                    display: grid;
                    grid-template-columns: repeat(4, 1fr);
                    gap: 12px;
                    margin: 18px 0;
                }
                .ai-trace-node {
                    border: 1px solid rgba(120,120,120,0.25);
                    border-radius: 14px;
                    padding: 12px;
                    background: rgba(120,120,120,0.04);
                }
                .ai-trace-node strong { display:block; margin-bottom: 4px; }
                .ai-trace-node span { color: rgba(120,120,120,0.95); font-size: 0.86rem; }
                @media (max-width: 900px) { .ai-trace-flow { grid-template-columns: 1fr; } }
                </style>
                <div class="ai-trace-flow">
                    <div class="ai-trace-node"><strong>1. Route intent</strong><span>Classify direct action vs retrieval question.</span></div>
                    <div class="ai-trace-node"><strong>2. Embed query</strong><span>Voyage converts text into vectors.</span></div>
                    <div class="ai-trace-node"><strong>3. Retrieve context</strong><span>Atlas Vector Search / MongoDB finds bookings and venues.</span></div>
                    <div class="ai-trace-node"><strong>4. Generate answer</strong><span>LLM drafts the final response from context.</span></div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            if steps:
                step_df = pd.DataFrame(steps)
                st.markdown("##### Step Latency")
                st.dataframe(
                    step_df[["step", "name", "engine", "detail", "latency_ms"]],
                    hide_index=True,
                    use_container_width=True,
                )
                st.markdown("##### Latency By Component")
                st.bar_chart(pd.DataFrame.from_dict(by_engine, orient="index", columns=["latency_ms"]))
            with st.expander("Raw trace JSON"):
                st.code(json.dumps(trace, indent=2, default=str), language="json")

    # ---- Performance scorecard ----
    with sub_perf:
        st.markdown(
            "Left column is **live** -- computed from real latency/outcome samples this app "
            "(and any running `locustfile.py`) just wrote to the `metrics_events` collection "
            "in the same Atlas cluster. Right column is reserved for the customer's "
            "Aurora + ORM + OpenSearch baseline metrics."
        )
        live = fetch_live_mongo_stats(window_minutes=5)
        mongo_stats = live or MONGO_REFERENCE_STATS
        legacy_stats = LEGACY_REFERENCE_STATS

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("##### MongoDB Atlas " + ("(live, last 5 min)" if live else "(reference)"))
            st.metric("p99 latency", f"{mongo_stats['p99']:.1f} ms" if mongo_stats['p99'] is not None else "--")
            mc1, mc2 = st.columns(2)
            mc1.metric("p50", f"{mongo_stats['p50']:.1f} ms" if mongo_stats['p50'] is not None else "--")
            mc2.metric("p95", f"{mongo_stats['p95']:.1f} ms" if mongo_stats['p95'] is not None else "--")
            mc1.metric("Error rate", f"{mongo_stats['error_rate_pct']:.1f}%")
            mc2.metric("Throughput", f"{mongo_stats['approx_rps']:,.0f} req/s")
            if live:
                st.caption(f"Based on {live['count']:,} requests captured in the last 5 minutes.")
            else:
                st.caption(MONGO_REFERENCE_STATS["note"])
        with c2:
            st.markdown("##### Aurora + ORM + OpenSearch (customer baseline)")
            st.metric("p99 latency", f"{legacy_stats['p99']:.1f} ms" if legacy_stats["p99"] is not None else "--")
            lc1, lc2 = st.columns(2)
            lc1.metric("p50", f"{legacy_stats['p50']:.1f} ms" if legacy_stats["p50"] is not None else "--")
            lc2.metric("p95", f"{legacy_stats['p95']:.1f} ms" if legacy_stats["p95"] is not None else "--")
            lc1.metric("Error rate", f"{legacy_stats['error_rate_pct']:.1f}%" if legacy_stats["error_rate_pct"] is not None else "--")
            lc2.metric("Throughput", f"{legacy_stats['approx_rps']:,.0f} req/s" if legacy_stats["approx_rps"] is not None else "--")
            st.caption(legacy_stats["note"])

        if mongo_stats["p99"] and legacy_stats["p99"]:
            multiplier = legacy_stats["p99"] / mongo_stats["p99"]
            st.success(f"At p99, MongoDB Atlas is currently ≈ {multiplier:.1f}x faster than the reference legacy stack.")

        chart_df = pd.DataFrame(
            {
                "MongoDB Atlas": [mongo_stats["p50"], mongo_stats["p95"], mongo_stats["p99"]],
                "Aurora + ORM + OpenSearch": [legacy_stats["p50"], legacy_stats["p95"], legacy_stats["p99"]],
            },
            index=["p50", "p95", "p99"],
        )
        st.bar_chart(chart_df)

        st.caption(
            "Want real numbers instead of the reference column? Run `locust -f locustfile.py`, "
            "open http://localhost:8089, point it at this cluster, and watch the left column "
            "update as load samples accumulate in `metrics_events`."
        )
