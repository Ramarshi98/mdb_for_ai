# AtlasTrips

A demo Streamlit app + Locust load test arguing that a single MongoDB Atlas
cluster (operational data + Atlas Search) beats a Postgres/Aurora + ORM +
OpenSearch stack at travel-booking scale: faster type-ahead search, no
JOIN-driven latency spikes, and one engine instead of two to operate.

## Repository structure

```
mdb_for_ai/
├── app.py                       # Streamlit app: Travel Search UI + Behind the Scenes demo tab
├── locustfile.py                # Load test: hits Atlas Search + single-doc reads + booking writes directly
├── requirements.txt
├── .env.example                 # Copy to .env and fill in
├── README.md
├── db/
│   ├── __init__.py
│   ├── connection.py            # Shared PyMongo client / collection getters (reads .env)
│   ├── schema.py                # Reference venue/user document shapes (plain dicts)
│   └── seed.py                  # Generates realistic global venues + users, seeds Atlas
└── search/
    └── atlas_search_index.json  # Atlas Search index definition (edgeGram autocomplete)
```

## Setup

1. **Install dependencies** (Python 3.11+):
   ```
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment**: copy `.env.example` to `.env` and fill in
   `MONGODB_ATLAS_URI` (and optionally `DB_NAME` / `COLLECTION_NAME`, which
   default to `atlastrips` / `venues`). Make sure your IP is allow-listed in
   Atlas Network Access.

3. **Seed demo data**:
   ```
   python -m db.seed --count 2000 --users 300 --reset
   ```
   This generates denormalized `venues` documents (hotels, airport lounges,
   events) spread across 15 real global airports/cities, plus `users` with
   embedded booking history. You can also re-run seeding from the app's
   sidebar ("Seed / reset demo data").

4. **Create the Atlas Search index**: in the Atlas UI, go to your cluster ->
   **Search** -> **Create Search Index** -> **JSON Editor**, and paste the
   contents of `search/atlas_search_index.json` (the `definition` key is what
   the JSON Editor expects; the index name should be `venues_autocomplete`
   to match `SEARCH_INDEX_NAME` in `.env`). This index powers `edgeGram`
   autocomplete on `name`, `region.city`, and `category`.

5. **Run the app**:
   ```
   streamlit run app.py
   ```

6. **Run the load test** (optional, generates live numbers for the
   Performance Scorecard tab):
   ```
   locust -f locustfile.py
   ```
   Open `http://localhost:8089`, set a user count / spawn rate, and start.
   Locust talks directly to the same Atlas cluster via PyMongo (the "Host"
   field in the Locust UI is unused for this test).

## What the app demonstrates

- **Faster search & autocomplete** -- the Travel Search tab queries Atlas
  Search's `autocomplete` operator on every keystroke; the Behind the Scenes
  tab shows the exact aggregation pipeline that just ran.
- **Reliability at peak** -- venue documents embed region, pricing,
  availability, and facilities, so rendering a result is one `find_one()` or
  one `$search` call, not a multi-table JOIN chain.
- **Unified workloads** -- booking writes (`update_one` with a positional
  `$` operator), search reads, and even the Locust load test's own latency
  telemetry all land in the same Atlas cluster (`metrics_events` collection),
  which is what feeds the live half of the Performance Scorecard.

The Aurora/OpenSearch comparison numbers in the Performance Scorecard are
labeled as an **illustrative reference baseline** for the demo narrative, not
a certified benchmark -- swap them for your own measurements if you want an
apples-to-apples comparison.
