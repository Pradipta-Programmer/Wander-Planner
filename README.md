# TravelGPT Prototype — Updated

## Running it

```bash
pip install fastapi uvicorn pydantic
python main.py
```
This starts the API on `http://127.0.0.1:8000`. Then open `index.html` in a
browser (or serve it with any static server, e.g. `python -m http.server 5500`).

If your API isn't at `http://127.0.0.1:8000/api`, set it before the page's
script runs, e.g. add before the closing `</head>`:
```html
<script>window.TRAVELGPT_API_BASE = "https://your-api.example.com/api";</script>
```

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `TRAVELGPT_ALLOWED_ORIGINS` | `http://localhost:5500,http://127.0.0.1:5500,http://localhost:3000,http://127.0.0.1:3000,http://localhost:8080,http://127.0.0.1:8080` | Comma-separated CORS allow-list. Add your deployed frontend's origin here. |
| `TRAVELGPT_DATA_FILE` | `sessions_store.json` next to `main.py` | Where itinerary sessions are persisted. |
| `TRAVELGPT_MAX_UNDO_HISTORY` | `25` | How many prior states are kept for Undo. |
| `TRAVELGPT_BUFFER_MIN` | `20` | Minimum gap (minutes) enforced between consecutive itinerary items. |
| `TRAVELGPT_HOST` / `TRAVELGPT_PORT` | `0.0.0.0` / `8000` | Only used when running `python main.py` directly. |

## What changed vs. the original prototype

**Replanning** — `/api/replan` now mutates the itinerary's actual timing
(not just its status), cascades delays to every later event, can handle
several disruption keywords in one description (e.g. "flight delayed and
heavy rain"), swaps in destination-specific alternatives for closures/weather,
and always operates on the itinerary as it currently stands — so a second
disruption stacks on top of the first instead of resetting it. Every response
includes a human-readable explanation of what changed and why.

**Chat** — `/api/chat` is now itinerary-aware: it reads the live session
(destination, days, budget, current items) to answer budget questions,
suggest cheaper swaps, recommend unseen attractions/restaurants (without
repeating itself), explain why something is scheduled, suggest packing
items and weather backups, and can edit the itinerary directly via natural
commands like *"remove the museum visit"*, *"add scuba diving"*, *"replace X
with Y"*, or *"change time of X to 5:00 PM"*.

**Map** — every itinerary item now carries `lat`/`lng` (real coordinates for
the 7 curated destinations, a deterministic synthetic layout for anything
else). The frontend renders a Leaflet map with colour-coded markers, a route
line between stops, and two-way sync: click a marker to highlight its board
row, click a row to fly to its marker.

**Budget** — a dedicated panel shows total budget, estimated cost, remaining
budget and usage %, with a progress bar and an explicit over-budget warning.

**UI** — delayed/changed rows get a coloured left border, a per-item "why
this changed" note, Undo/Reset buttons, drag-and-drop reordering within a
day (server recalculates times to stay chronological), and mobile-responsive
breakpoints. All inline styles used by JS-generated markup were moved into
CSS classes.

**Backend hardening** — request validation (Pydantic constraints + friendly
error messages), try/catch-safe endpoints returning proper HTTP errors,
CORS restricted to a configurable allow-list instead of `*`, all URLs/limits
moved to environment variables, and itineraries persisted to a JSON-file
session store (keyed by `session_id`) instead of living only in the request.

## Known simplifications (prototype scope)

- Persistence is a single JSON file, not a real database — fine for a
  prototype/demo, swap `_load_db`/`_save_db` for a real DB call if needed.
- The "AI" is a fast, deterministic rules engine (as the original was) rather
  than a hosted LLM call — there's no LLM API wired in. The reasoning strings
  are generated from the same disruption/preference data driving the itinerary
  itself, so they stay consistent, but replacing `replan_items`/`handle_chat`
  with real model calls is a natural next step if you want more open-ended
  language understanding.
