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

Tab 2, "Behind the Scenes": the architecture diagram, the exact MQL that
just ran, and a performance scorecard that blends LIVE latency samples
(captured from this app + any running locustfile.py traffic, all written to
the same Atlas cluster) against a static reference baseline for the legacy
relational + search-silo stack.
"""

import json
import random
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from st_keyup import st_keyup

from db.connection import (
    COLLECTION_NAME,
    DB_NAME,
    MONGODB_ATLAS_URI,
    SEARCH_INDEX_NAME,
    get_db,
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

# ---------------------------------------------------------------------------
# Guard: stop early with a friendly message if Atlas isn't configured yet.
# ---------------------------------------------------------------------------
if not MONGODB_ATLAS_URI:
    st.title("✈️ AtlasTrips")
    st.warning(
        "**MONGODB_ATLAS_URI is not set.** Copy `.env.example` to `.env`, "
        "paste in your Atlas connection string, then restart the app.\n\n"
        "Also make sure you've run `python -m db.seed` and created the "
        "Atlas Search index from `search/atlas_search_index.json`."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
defaults = {
    "selected_venue_id": None,
    "my_bookings": [],
    "query_input": "",
    "last_pipeline": None,
    "last_action_label": "No action yet -- run a search or open a venue to see its live MQL here.",
    "search_error": None,
}
for k, v in defaults.items():
    st.session_state.setdefault(k, v)


def select_suggestion(venue_id: str, venue_name: str) -> None:
    st.session_state.selected_venue_id = venue_id
    st.session_state.query_input = venue_name

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


# Reference baseline for the legacy stack. These are illustrative narrative
# figures for the demo (not a certified third-party benchmark) -- swap in
# your own Aurora measurements for an apples-to-apples comparison.
LEGACY_REFERENCE_STATS = {
    "label": "Aurora (Postgres) + ORM + OpenSearch sync — reference baseline",
    "p50": 64, "p95": 142, "p99": 185,
    "error_rate_pct": 5.4, "approx_rps": 100_000,
    "note": "Illustrative reference figures for demo narration, modeled on commonly "
            "reported JOIN-heavy ORM + search-sync patterns at peak load. Replace with "
            "your own Aurora baseline for a precise comparison.",
}
MONGO_REFERENCE_STATS = {
    "label": "MongoDB Atlas ($search + operational, unified) — reference baseline",
    "p50": 5, "p95": 9, "p99": 12,
    "error_rate_pct": 0.0, "approx_rps": 100_000,
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
    try:
        get_db()["demo_bookings"].insert_one(dict(booking))
    except Exception:
        pass
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

tab_ui, tab_internals = st.tabs(["\U0001F9ED Travel Search", "\U0001F6E0️ Behind the Scenes"])

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

    if st.session_state.my_bookings:
        with st.expander(f"\U0001F9F3 This session's bookings ({len(st.session_state.my_bookings)})"):
            st.dataframe(pd.DataFrame(st.session_state.my_bookings), hide_index=True, use_container_width=True)

# ===========================================================================
# TAB 2 -- Behind the Scenes
# ===========================================================================
with tab_internals:
    sub_arch, sub_query, sub_perf = st.tabs(["\U0001F5FA️ Architecture", "\U0001F50E MQL Query Inspector", "\U0001F4CA Performance Scorecard"])

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
                gap: 18px;
                margin-top: 18px;
            }
            .arch-lane {
                border: 1px solid rgba(120,120,120,0.25);
                border-radius: 16px;
                padding: 18px;
                background: rgba(120,120,120,0.04);
            }
            .arch-lane h4 { margin: 0 0 14px 0; }
            .arch-node {
                border-radius: 12px;
                padding: 12px 14px;
                margin: 10px 0;
                font-weight: 650;
                line-height: 1.25;
                box-shadow: 0 1px 8px rgba(0,0,0,0.06);
            }
            .arch-node span { display: block; font-weight: 400; font-size: 0.88rem; margin-top: 4px; }
            .arch-neutral { background: #eef1f5; border: 1px solid #8a93a3; color: #2b3340; }
            .arch-mongo { background: #e7f7ee; border: 2px solid #13aa52; color: #0a3d22; }
            .arch-legacy { background: #fdeceb; border: 2px solid #c0392b; color: #5a1f18; }
            .arch-arrow { text-align: center; color: rgba(120,120,120,0.9); font-weight: 800; }
            .arch-split {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 10px;
            }
            @media (max-width: 900px) {
                .arch-grid, .arch-split { grid-template-columns: 1fr; }
            }
            </style>
            <div class="arch-grid">
                <div class="arch-lane">
                    <h4>MongoDB Atlas path</h4>
                    <div class="arch-node arch-neutral">User request</div>
                    <div class="arch-arrow">v</div>
                    <div class="arch-node arch-neutral">Streamlit App</div>
                    <div class="arch-arrow">v</div>
                    <div class="arch-node arch-mongo">
                        MongoDB Atlas
                        <span>Operational data + Atlas Search in one unified engine</span>
                    </div>
                    <div class="arch-arrow">v</div>
                    <div class="arch-node arch-mongo">
                        One document back
                        <span>Region + pricing + availability + facilities embedded</span>
                    </div>
                </div>
                <div class="arch-lane">
                    <h4>Aurora + ORM + OpenSearch path</h4>
                    <div class="arch-node arch-neutral">User request</div>
                    <div class="arch-arrow">v</div>
                    <div class="arch-node arch-neutral">Web / App Server</div>
                    <div class="arch-arrow">splits into two systems</div>
                    <div class="arch-split">
                        <div>
                            <div class="arch-node arch-legacy">ORM layer<span>e.g. SQLAlchemy</span></div>
                            <div class="arch-arrow">v</div>
                            <div class="arch-node arch-legacy">Aurora / Postgres<span>Venues, regions, pricing, availability, facilities tables</span></div>
                            <div class="arch-arrow">JOIN x4</div>
                            <div class="arch-node arch-legacy">Assembled row set</div>
                        </div>
                        <div>
                            <div class="arch-node arch-legacy">CDC / Debezium sync</div>
                            <div class="arch-arrow">v</div>
                            <div class="arch-node arch-legacy">OpenSearch cluster</div>
                            <div class="arch-arrow">v</div>
                            <div class="arch-node arch-legacy">Search hits<span>Possible index lag</span></div>
                        </div>
                    </div>
                    <div class="arch-arrow">v</div>
                    <div class="arch-node arch-legacy">App-side merge</div>
                    <div class="arch-arrow">v</div>
                    <div class="arch-node arch-legacy">Response<span>More hops, more failure modes</span></div>
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

    # ---- Performance scorecard ----
    with sub_perf:
        st.markdown(
            "Left column is **live** -- computed from real latency/outcome samples this app "
            "(and any running `locustfile.py`) just wrote to the `metrics_events` collection "
            "in the same Atlas cluster. Right column is a static reference baseline for the "
            "legacy stack (see caption)."
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
            st.markdown("##### Aurora + ORM + OpenSearch (reference)")
            st.metric("p99 latency", f"{legacy_stats['p99']:.1f} ms")
            lc1, lc2 = st.columns(2)
            lc1.metric("p50", f"{legacy_stats['p50']:.1f} ms")
            lc2.metric("p95", f"{legacy_stats['p95']:.1f} ms")
            lc1.metric("Error rate", f"{legacy_stats['error_rate_pct']:.1f}%")
            lc2.metric("Throughput", f"{legacy_stats['approx_rps']:,.0f} req/s")
            st.caption(legacy_stats["note"])

        if mongo_stats["p99"]:
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
