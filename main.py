
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

# =========================================================
#  CONFIGURATION (env-driven, sensible local defaults)
# =========================================================
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "TRAVELGPT_ALLOWED_ORIGINS",
        "http://localhost:5500,http://127.0.0.1:5500,"
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:8080,http://127.0.0.1:8080",
    ).split(",")
    if o.strip()
]
DATA_FILE = os.environ.get(
    "TRAVELGPT_DATA_FILE", os.path.join(tempfile.gettempdir(), "travelgpt_sessions_store.json")
)
MAX_UNDO_HISTORY = int(os.environ.get("TRAVELGPT_MAX_UNDO_HISTORY", "25"))
DEFAULT_BUFFER_MIN = int(os.environ.get("TRAVELGPT_BUFFER_MIN", "20"))

app = FastAPI(title="TravelGPT API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# =========================================================
#  PERSISTENCE  (file-backed session store)
# =========================================================
_store_lock = threading.Lock()


def _load_db() -> Dict:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_db(db: Dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f)
    os.replace(tmp, DATA_FILE)


def get_session(session_id: str) -> Dict:
    with _store_lock:
        db = _load_db()
    session = db.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found. Please generate a new itinerary.")
    return session


def save_session(session_id: str, session: Dict) -> None:
    with _store_lock:
        db = _load_db()
        db[session_id] = session
        _save_db(db)


# =========================================================
#  REQUEST / RESPONSE MODELS
# =========================================================
class TripSetup(BaseModel):
    destination: str = Field(..., min_length=1, max_length=120)
    days: int = Field(..., ge=1, le=30)
    budget: float = Field(..., gt=0, le=10_000_000)
    preferences: List[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=500)

    @field_validator("destination")
    @classmethod
    def _strip_destination(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Destination cannot be blank.")
        return v

    @field_validator("preferences")
    @classmethod
    def _clean_prefs(cls, v: List[str]) -> List[str]:
        return [p.strip().lower() for p in v if isinstance(p, str) and p.strip()][:12]


class DisruptionRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1, max_length=300)


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1, max_length=500)


class SessionRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class ReorderRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    day: int = Field(..., ge=1)
    ordered_ids: List[str] = Field(..., min_length=1)


# =========================================================
#  DESTINATION KNOWLEDGE BASE (all costs in INR)
# =========================================================
DESTINATIONS: Dict[str, Dict] = {
    "goa": {
        "display": "Goa",
        "transport_cost": 8000,
        "hotel_cost": 4000,
        "hotel_location": "North Goa",
        "best_time": "November to February, when the weather is cool and dry",
        "currency_note": "INR is used everywhere; UPI is widely accepted even in beach shacks",
        "local_tip": "Rent a scooter to get around \u2014 taxis are expensive and distances are spread out",
        "activities": [
            {"title": "Heritage Fort Tour", "location": "Fort Aguada", "cost": 500, "tags": ["history"]},
            {"title": "Scuba Diving", "location": "Grand Island", "cost": 3500, "tags": ["adventure"]},
            {"title": "Sunset at Candolim Beach", "location": "Candolim", "cost": 0, "tags": ["beaches", "relaxed"]},
            {"title": "Anjuna Flea Market Walk", "location": "Anjuna", "cost": 300, "tags": ["food", "nightlife"]},
            {"title": "Beach Shack Nightlife", "location": "Baga", "cost": 1200, "tags": ["nightlife"]},
            {"title": "Goa State Museum", "location": "Panjim", "cost": 400, "tags": ["museums", "history"]},
            {"title": "Dudhsagar Waterfall Trek", "location": "Dudhsagar", "cost": 1800, "tags": ["nature", "adventure"]},
        ],
        "food": [
            {"title": "Beachside Seafood Lunch", "location": "Calangute", "cost": 900},
            {"title": "Fine Dining", "location": "Calangute", "cost": 1500},
            {"title": "Beachside Brunch", "location": "Anjuna", "cost": 800},
            {"title": "Indoor Goan Thali Dinner", "location": "Panjim", "cost": 700},
        ],
    },
    "kolkata": {
        "display": "Kolkata",
        "transport_cost": 6000,
        "hotel_cost": 3000,
        "hotel_location": "Park Street",
        "best_time": "October to March, before the humidity builds up",
        "currency_note": "INR only; carry some cash for trams and street stalls",
        "local_tip": "The metro is the fastest way across town and avoids traffic",
        "activities": [
            {"title": "Victoria Memorial Tour", "location": "Maidan", "cost": 500, "tags": ["history", "museums"]},
            {"title": "Howrah Bridge & Ganges Walk", "location": "Howrah", "cost": 0, "tags": ["relaxed", "nature"]},
            {"title": "Indian Museum Visit", "location": "Jawaharlal Nehru Road", "cost": 300, "tags": ["museums", "history"]},
            {"title": "Kumartuli Potters' Quarter", "location": "Kumartuli", "cost": 200, "tags": ["history"]},
            {"title": "Park Street Nightlife", "location": "Park Street", "cost": 1300, "tags": ["nightlife"]},
            {"title": "Dakshineswar Kali Temple", "location": "Dakshineswar", "cost": 150, "tags": ["history", "relaxed"]},
            {"title": "Eco Park Nature Walk", "location": "New Town", "cost": 100, "tags": ["nature", "relaxed"]},
        ],
        "food": [
            {"title": "Bengali Thali Lunch", "location": "Park Street", "cost": 600},
            {"title": "Street Food at New Market", "location": "New Market", "cost": 350},
            {"title": "Fine Dining", "location": "Park Street", "cost": 1400},
            {"title": "Rooftop Caf\u00e9 Breakfast", "location": "Ballygunge", "cost": 400},
        ],
    },
    "jaipur": {
        "display": "Jaipur",
        "transport_cost": 6500,
        "hotel_cost": 3200,
        "hotel_location": "Pink City",
        "best_time": "October to March, avoiding the peak summer heat",
        "currency_note": "INR only; bargaining is normal in the bazaars",
        "local_tip": "Start fort visits early \u2014 Amer Fort gets very hot and crowded by midday",
        "activities": [
            {"title": "Amber Fort Tour", "location": "Amer", "cost": 700, "tags": ["history"]},
            {"title": "Hawa Mahal Photo Walk", "location": "Pink City", "cost": 200, "tags": ["history", "relaxed"]},
            {"title": "City Palace & Museum", "location": "Pink City", "cost": 500, "tags": ["museums", "history"]},
            {"title": "Camel Safari at Dunes", "location": "Outskirts", "cost": 2200, "tags": ["adventure"]},
            {"title": "Johari Bazaar Shopping", "location": "Johari Bazaar", "cost": 300, "tags": ["food"]},
            {"title": "Rooftop Nightlife", "location": "Pink City", "cost": 1200, "tags": ["nightlife"]},
            {"title": "Nahargarh Sunset Point", "location": "Nahargarh Hills", "cost": 150, "tags": ["nature", "relaxed"]},
        ],
        "food": [
            {"title": "Rajasthani Thali Lunch", "location": "Pink City", "cost": 650},
            {"title": "Street Food at Bapu Bazaar", "location": "Bapu Bazaar", "cost": 300},
            {"title": "Fine Dining", "location": "Civil Lines", "cost": 1500},
            {"title": "Rooftop Breakfast", "location": "Pink City", "cost": 400},
        ],
    },
    "manali": {
        "display": "Manali",
        "transport_cost": 7000,
        "hotel_cost": 2800,
        "hotel_location": "Old Manali",
        "best_time": "March to June for pleasant weather, December to February for snow",
        "currency_note": "INR only; ATMs are limited outside Mall Road, carry cash",
        "local_tip": "Roads to Rohtang Pass need a permit and can close without notice \u2014 always have a backup plan",
        "activities": [
            {"title": "Solang Valley Adventure Sports", "location": "Solang Valley", "cost": 2500, "tags": ["adventure"]},
            {"title": "Hadimba Temple Visit", "location": "Old Manali", "cost": 0, "tags": ["history", "relaxed"]},
            {"title": "Rohtang Pass Excursion", "location": "Rohtang Pass", "cost": 1800, "tags": ["nature", "adventure"]},
            {"title": "Old Manali Caf\u00e9 Hopping", "location": "Old Manali", "cost": 500, "tags": ["food", "relaxed"]},
            {"title": "Riverside Bonfire Nightlife", "location": "Beas Riverside", "cost": 900, "tags": ["nightlife"]},
            {"title": "Naggar Castle & Art Gallery", "location": "Naggar", "cost": 300, "tags": ["museums", "history"]},
            {"title": "Pine Forest Nature Walk", "location": "Old Manali", "cost": 100, "tags": ["nature", "relaxed"]},
        ],
        "food": [
            {"title": "Himachali Thali Lunch", "location": "Mall Road", "cost": 500},
            {"title": "Caf\u00e9 Lunch", "location": "Old Manali", "cost": 450},
            {"title": "Bonfire Dinner", "location": "Riverside", "cost": 800},
            {"title": "Rooftop Breakfast", "location": "Old Manali", "cost": 350},
        ],
    },
    "kerala": {
        "display": "Kerala",
        "transport_cost": 7500,
        "hotel_cost": 3500,
        "hotel_location": "Fort Kochi",
        "best_time": "September to March, outside the monsoon",
        "currency_note": "INR only; small towns may not accept cards",
        "local_tip": "Book the houseboat cruise a day ahead in peak season, they sell out fast",
        "activities": [
            {"title": "Backwater Houseboat Cruise", "location": "Alleppey", "cost": 3000, "tags": ["nature", "relaxed"]},
            {"title": "Fort Kochi Heritage Walk", "location": "Fort Kochi", "cost": 300, "tags": ["history"]},
            {"title": "Munnar Tea Garden Tour", "location": "Munnar", "cost": 800, "tags": ["nature"]},
            {"title": "Kathakali Dance Show", "location": "Fort Kochi", "cost": 500, "tags": ["museums", "history"]},
            {"title": "Beach Evening at Fort Kochi", "location": "Fort Kochi Beach", "cost": 0, "tags": ["beaches", "relaxed"]},
            {"title": "White Water Rafting", "location": "Kallar", "cost": 2200, "tags": ["adventure"]},
            {"title": "Marine Drive Nightlife", "location": "Kochi", "cost": 1000, "tags": ["nightlife"]},
        ],
        "food": [
            {"title": "Kerala Sadhya Lunch", "location": "Fort Kochi", "cost": 550},
            {"title": "Seafood Dinner", "location": "Marine Drive", "cost": 1300},
            {"title": "Houseboat Onboard Meal", "location": "Alleppey", "cost": 700},
            {"title": "Caf\u00e9 Breakfast", "location": "Fort Kochi", "cost": 350},
        ],
    },
    "mumbai": {
        "display": "Mumbai",
        "transport_cost": 6000,
        "hotel_cost": 4500,
        "hotel_location": "Colaba",
        "best_time": "November to February, cool and dry",
        "currency_note": "INR only; UPI accepted almost everywhere including taxis",
        "local_tip": "Use the local trains outside rush hour \u2014 they're by far the fastest way across the city",
        "activities": [
            {"title": "Gateway of India & Colaba Walk", "location": "Colaba", "cost": 0, "tags": ["history", "relaxed"]},
            {"title": "Elephanta Caves Ferry Tour", "location": "Elephanta Island", "cost": 1200, "tags": ["history"]},
            {"title": "Marine Drive Sunset", "location": "Marine Drive", "cost": 0, "tags": ["relaxed", "beaches"]},
            {"title": "CSMVS Museum Visit", "location": "Fort", "cost": 400, "tags": ["museums", "history"]},
            {"title": "Bandra Nightlife", "location": "Bandra", "cost": 1800, "tags": ["nightlife"]},
            {"title": "Sanjay Gandhi National Park Trek", "location": "Borivali", "cost": 300, "tags": ["nature", "adventure"]},
            {"title": "Juhu Beach Evening", "location": "Juhu", "cost": 200, "tags": ["beaches"]},
        ],
        "food": [
            {"title": "Street Food at Mohammed Ali Road", "location": "South Mumbai", "cost": 400},
            {"title": "Fine Dining", "location": "Bandra", "cost": 2000},
            {"title": "Vada Pav & Local Bites", "location": "Colaba", "cost": 250},
            {"title": "Rooftop Breakfast", "location": "Bandra", "cost": 500},
        ],
    },
    "delhi": {
        "display": "Delhi",
        "transport_cost": 5500,
        "hotel_cost": 3500,
        "hotel_location": "Connaught Place",
        "best_time": "October to March, avoiding peak summer heat",
        "currency_note": "INR only; metro cards / UPI cover most transport",
        "local_tip": "Old Delhi is best explored on foot or by cycle-rickshaw \u2014 the lanes are too narrow for cars",
        "activities": [
            {"title": "Red Fort & Chandni Chowk Walk", "location": "Old Delhi", "cost": 300, "tags": ["history"]},
            {"title": "India Gate Evening", "location": "Central Delhi", "cost": 0, "tags": ["relaxed", "history"]},
            {"title": "National Museum Visit", "location": "Central Delhi", "cost": 400, "tags": ["museums", "history"]},
            {"title": "Hauz Khas Nightlife", "location": "Hauz Khas", "cost": 1500, "tags": ["nightlife"]},
            {"title": "Lodhi Garden Nature Walk", "location": "Lodhi Road", "cost": 0, "tags": ["nature", "relaxed"]},
            {"title": "Qutub Minar Tour", "location": "Mehrauli", "cost": 500, "tags": ["history"]},
            {"title": "Chandni Chowk Street Food Trail", "location": "Old Delhi", "cost": 400, "tags": ["food"]},
        ],
        "food": [
            {"title": "Street Food at Chandni Chowk", "location": "Old Delhi", "cost": 400},
            {"title": "Fine Dining", "location": "Connaught Place", "cost": 1800},
            {"title": "Caf\u00e9 Lunch", "location": "Hauz Khas", "cost": 600},
            {"title": "Rooftop Breakfast", "location": "Connaught Place", "cost": 400},
        ],
    },
}

CITY_ALIASES = {
    "goa": "goa",
    "kolkata": "kolkata", "calcutta": "kolkata",
    "jaipur": "jaipur",
    "manali": "manali",
    "kerala": "kerala", "munnar": "kerala", "kochi": "kerala", "cochin": "kerala", "alleppey": "kerala",
    "mumbai": "mumbai", "bombay": "mumbai",
    "delhi": "delhi", "new delhi": "delhi",
}

# Approximate real-world coordinates for curated destinations, keyed by the
# exact "location" string used in DESTINATIONS above, plus special "hotel"
# and "airport" anchors.
DEST_COORDS: Dict[str, Dict[str, Tuple[float, float]]] = {
    "goa": {
        "hotel": (15.5500, 73.7600), "airport": (15.3808, 73.8314),
        "Fort Aguada": (15.4925, 73.7738), "Grand Island": (15.3892, 73.9080),
        "Candolim": (15.5185, 73.7654), "Anjuna": (15.5734, 73.7401),
        "Baga": (15.5553, 73.7517), "Panjim": (15.4909, 73.8278),
        "Dudhsagar": (15.3144, 74.3142), "Calangute": (15.5439, 73.7553),
    },
    "kolkata": {
        "hotel": (22.5535, 88.3524), "airport": (22.6547, 88.4467),
        "Maidan": (22.5448, 88.3426), "Howrah": (22.5851, 88.3468),
        "Jawaharlal Nehru Road": (22.5579, 88.3512), "Kumartuli": (22.5958, 88.3600),
        "Park Street": (22.5535, 88.3524), "Dakshineswar": (22.6547, 88.3577),
        "New Town": (22.6076, 88.4645), "New Market": (22.5626, 88.3529),
        "Ballygunge": (22.5288, 88.3654),
    },
    "jaipur": {
        "hotel": (26.9239, 75.8267), "airport": (26.8242, 75.8122),
        "Amer": (26.9855, 75.8513), "Pink City": (26.9239, 75.8267),
        "Outskirts": (26.9500, 75.7500), "Johari Bazaar": (26.9200, 75.8250),
        "Nahargarh Hills": (26.9373, 75.8154), "Bapu Bazaar": (26.9175, 75.8223),
        "Civil Lines": (26.9100, 75.7873),
    },
    "manali": {
        "hotel": (32.2515, 77.1789), "airport": (31.8767, 77.1553),
        "Solang Valley": (32.3172, 77.1568), "Old Manali": (32.2515, 77.1789),
        "Rohtang Pass": (32.3729, 77.2497), "Beas Riverside": (32.2450, 77.1850),
        "Naggar": (32.1257, 77.1698), "Mall Road": (32.2432, 77.1892),
        "Riverside": (32.2450, 77.1850),
    },
    "kerala": {
        "hotel": (9.9658, 76.2422), "airport": (10.1520, 76.4019),
        "Alleppey": (9.4981, 76.3388), "Fort Kochi": (9.9658, 76.2422),
        "Munnar": (10.0889, 77.0595), "Fort Kochi Beach": (9.9633, 76.2380),
        "Kallar": (8.9500, 77.1500), "Kochi": (9.9750, 76.2810),
        "Marine Drive": (9.9750, 76.2810),
    },
    "mumbai": {
        "hotel": (18.9067, 72.8147), "airport": (19.0896, 72.8656),
        "Colaba": (18.9067, 72.8147), "Elephanta Island": (18.9633, 72.9315),
        "Marine Drive": (18.9440, 72.8236), "Fort": (18.9269, 72.8328),
        "Bandra": (19.0596, 72.8295), "Borivali": (19.2147, 72.9106),
        "Juhu": (19.1075, 72.8263), "South Mumbai": (18.9490, 72.8258),
    },
    "delhi": {
        "hotel": (28.6315, 77.2167), "airport": (28.5562, 77.1000),
        "Old Delhi": (28.6562, 77.2410), "Central Delhi": (28.6129, 77.2295),
        "Hauz Khas": (28.5535, 77.1936), "Lodhi Road": (28.5931, 77.2197),
        "Mehrauli": (28.5245, 77.1855), "Connaught Place": (28.6315, 77.2167),
    },
}
DEFAULT_CENTER = (28.6139, 77.2090)  # used for destinations outside the curated set


def _jitter(base: Tuple[float, float], seed: str, scale: float = 0.035) -> Tuple[float, float]:
    """Deterministic pseudo-random offset so unrecognised locations still get
    a stable, spread-out position on the map instead of stacking exactly."""
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()
    dx = (int(h[:8], 16) / 0xFFFFFFFF - 0.5) * 2 * scale
    dy = (int(h[8:16], 16) / 0xFFFFFFFF - 0.5) * 2 * scale
    return round(base[0] + dx, 5), round(base[1] + dy, 5)


def get_coords(dest_key: str, location_name: str, profile: Dict) -> Tuple[float, float]:
    table = DEST_COORDS.get(dest_key, {})
    center = table.get("hotel", DEFAULT_CENTER)
    if location_name == profile["hotel_location"]:
        return table.get("hotel", center)
    if location_name == "Airport":
        return table.get("airport", _jitter(center, dest_key + "airport", 0.15))
    if location_name in table:
        return table[location_name]
    return _jitter(center, dest_key + location_name)


def build_generic_profile(display: str) -> Dict:
    """Fallback profile for any destination not in the curated knowledge base,
    so ANY city typed in still gets a destination-specific plan."""
    return {
        "display": display,
        "transport_cost": 6000,
        "hotel_cost": 3000,
        "hotel_location": f"{display} City Center",
        "best_time": "Shoulder season generally offers the best weather and prices",
        "currency_note": "Local currency and INR-linked payment apps may not both be accepted \u2014 carry some cash",
        "local_tip": "Ask your hotel front desk for the most current local recommendations",
        "activities": [
            {"title": f"Guided City Tour of {display}", "location": "City Center", "cost": 800, "tags": ["history"]},
            {"title": "Local Market & Shopping Walk", "location": "Old Market", "cost": 400, "tags": ["food"]},
            {"title": "Sunset Viewpoint Visit", "location": "Scenic Point", "cost": 200, "tags": ["nature", "relaxed"]},
            {"title": "Adventure Sports Session", "location": "Adventure Park", "cost": 2500, "tags": ["adventure"]},
            {"title": "Museum & Heritage Walk", "location": "Heritage Quarter", "cost": 300, "tags": ["museums", "history"]},
            {"title": "Nightlife & Live Music", "location": "Downtown", "cost": 1500, "tags": ["nightlife"]},
            {"title": "Nature Trail Walk", "location": "City Outskirts", "cost": 100, "tags": ["nature", "relaxed"]},
        ],
        "food": [
            {"title": "Local Cuisine Lunch", "location": "City Center", "cost": 600},
            {"title": "Fine Dining Experience", "location": "Downtown", "cost": 1500},
            {"title": "Street Food Trail", "location": "Old Town", "cost": 400},
            {"title": "Rooftop Caf\u00e9 Breakfast", "location": "City Center", "cost": 350},
        ],
    }


def resolve_destination(destination: str) -> Tuple[str, Dict]:
    d = (destination or "").lower()
    for alias, key in CITY_ALIASES.items():
        if alias in d:
            return key, DESTINATIONS[key]
    raw = (destination or "").strip()
    display = raw.split(",")[0].strip().title() if raw else "Your Destination"
    key = "generic:" + display.lower()
    return key, build_generic_profile(display)


def rank_pool(pool: List[Dict], preferences: List[str]) -> List[Dict]:
    """Preference-weighted ranking: items matching more selected preferences
    sort first, so generation is strongly steered by what the user picked."""
    pref_set = set(preferences or [])
    return sorted(pool, key=lambda a: (-len(pref_set & set(a.get("tags", []))), a["title"]))


# =========================================================
#  TIME HELPERS
# =========================================================
DURATION_BY_TYPE = {"transport": 90, "hotel": 30, "activity": 150, "food": 75}


def time_to_minutes(t: str) -> int:
    t = t.strip().upper()
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", t)
    if not m:
        return 9 * 60
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return h * 60 + mi


def minutes_to_time(m: int) -> str:
    m = max(0, m) % (24 * 60)
    h, mi = divmod(m, 60)
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{mi:02d} {ap}"


def mk(dest_key: str, profile: Dict, time_str: str, day: int, title: str, location: str,
       type_: str, cost: float, status: str = "on-time") -> Dict:
    lat, lng = get_coords(dest_key, location, profile)
    return {
        "id": uuid.uuid4().hex[:10],
        "time": time_str,
        "minutes": time_to_minutes(time_str),
        "day": day,
        "title": title,
        "location": location,
        "type": type_,
        "status": status,
        "cost": round(cost),
        "duration": DURATION_BY_TYPE.get(type_, 90),
        "lat": lat,
        "lng": lng,
        "reason": "",
    }


def recompute_schedule(items: List[Dict], buffer_min: int = DEFAULT_BUFFER_MIN) -> List[str]:
    """Walk each day in time order and push any item that would overlap
    (or start before) the previous item's end + buffer forward in time.
    Returns the ids of items whose time actually moved."""
    shifted: List[str] = []
    days = sorted(set(i["day"] for i in items))
    for day in days:
        day_items = sorted([i for i in items if i["day"] == day], key=lambda i: i["minutes"])
        prev_end = None
        for item in day_items:
            start = item["minutes"]
            if prev_end is not None and start < prev_end + buffer_min:
                new_start = prev_end + buffer_min
                if new_start != start:
                    item["minutes"] = new_start
                    item["time"] = minutes_to_time(new_start)
                    if item["status"] == "on-time":
                        item["status"] = "changed"
                    shifted.append(item["id"])
            prev_end = item["minutes"] + item.get("duration", 60)
    return shifted


def sort_items(items: List[Dict]) -> List[Dict]:
    return sorted(items, key=lambda i: (i["day"], i["minutes"]))


# =========================================================
#  ITINERARY GENERATION
# =========================================================
def build_items(dest_key: str, profile: Dict, days: int, preferences: List[str]) -> List[Dict]:
    activities = rank_pool(profile["activities"], preferences)
    foods = profile["food"]

    act_i = [0]
    food_i = [0]

    def next_act():
        a = activities[act_i[0] % len(activities)]
        act_i[0] += 1
        return a

    def next_food():
        f = foods[food_i[0] % len(foods)]
        food_i[0] += 1
        return f

    items: List[Dict] = []
    display = profile["display"]
    M = lambda *a, **kw: mk(dest_key, profile, *a, **kw)

    if days <= 1:
        items.append(M("8:00 AM", 1, f"Flight to {display}", "Airport", "transport", profile["transport_cost"]))
        a1 = next_act()
        items.append(M("10:00 AM", 1, a1["title"], a1["location"], "activity", a1["cost"]))
        f1 = next_food()
        items.append(M("1:00 PM", 1, f1["title"], f1["location"], "food", f1["cost"]))
        a2 = next_act()
        items.append(M("3:30 PM", 1, a2["title"], a2["location"], "activity", a2["cost"]))
        items.append(M("7:00 PM", 1, f"Flight from {display}", "Airport", "transport", profile["transport_cost"]))
        return items

    nights = days - 1
    items.append(M("9:00 AM", 1, f"Flight to {display}", "Airport", "transport", profile["transport_cost"]))
    items.append(M("1:00 PM", 1, f"Hotel Check-in ({nights} night{'s' if nights > 1 else ''})",
                   profile["hotel_location"], "hotel", profile["hotel_cost"] * nights))
    a = next_act()
    items.append(M("4:00 PM", 1, a["title"], a["location"], "activity", a["cost"]))
    f = next_food()
    items.append(M("8:00 PM", 1, f["title"], f["location"], "food", f["cost"]))

    for day in range(2, days):
        a1 = next_act()
        items.append(M("9:30 AM", day, a1["title"], a1["location"], "activity", a1["cost"]))
        f1 = next_food()
        items.append(M("1:00 PM", day, f1["title"], f1["location"], "food", f1["cost"]))
        a2 = next_act()
        items.append(M("4:00 PM", day, a2["title"], a2["location"], "activity", a2["cost"]))
        f2 = next_food()
        items.append(M("8:00 PM", day, f2["title"], f2["location"], "food", f2["cost"]))

    a = next_act()
    items.append(M("9:30 AM", days, a["title"], a["location"], "activity", a["cost"]))
    f = next_food()
    items.append(M("1:00 PM", days, f["title"], f["location"], "food", f["cost"]))
    items.append(M("6:00 PM", days, f"Flight from {display}", "Airport", "transport", profile["transport_cost"]))

    return items


# =========================================================
#  REPLANNING ENGINE
# =========================================================
def _extract_delay_minutes(desc: str) -> int:
    m = re.search(r"(\d+)\s*hour", desc)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*(?:min|minute)", desc)
    if m:
        return int(m.group(1))
    return 120  # default assumption if no number was given


def _used_titles(items: List[Dict]) -> set:
    return {i["title"] for i in items}


def _apply_flight_delay(items: List[Dict], profile: Dict, desc: str, notes: List[str]) -> None:
    delay = _extract_delay_minutes(desc)
    hours = round(delay / 60, 1)
    # Delay whichever transport leg the description implies; default to the outbound leg.
    target_day = 1
    if "return" in desc or "last day" in desc or "flight back" in desc or "flight from" in desc:
        target_day = max(i["day"] for i in items)
    transport_items = [i for i in items if i["type"] == "transport" and i["day"] == target_day]
    for it in transport_items:
        it["minutes"] += delay
        it["time"] = minutes_to_time(it["minutes"])
        it["status"] = "delayed"
        if "(Delayed" not in it["title"]:
            it["title"] = re.sub(r"\s*\(Delayed.*?\)", "", it["title"]) + f" (Delayed {hours}h)"
        it["reason"] = f"Pushed back {hours}h because of the reported flight delay."
    notes.append(
        f"Flight on day {target_day} is delayed by {hours} hour(s). Everything scheduled after it on that day "
        f"has been shifted later to keep a realistic gap, and check-in / activity timings were recalculated "
        f"so nothing overlaps."
    )


def _apply_weather(items: List[Dict], profile: Dict, desc: str, notes: List[str]) -> None:
    outdoor_tags = {"beaches", "nature", "adventure"}
    indoor_pool = [
        a for a in profile["activities"]
        if (set(a.get("tags", [])) & {"museums", "history", "relaxed"})
        and not (set(a.get("tags", [])) & outdoor_tags)
    ]
    used = _used_titles(items)
    swapped = []
    r_i = 0
    for it in items:
        if it["type"] != "activity":
            continue
        match = next((a for a in profile["activities"] if a["title"] == it["title"]), None)
        if match and set(match.get("tags", [])) & outdoor_tags:
            candidates = [a for a in indoor_pool if a["title"] not in used]
            if not candidates:
                candidates = indoor_pool
            if not candidates:
                continue
            repl = candidates[r_i % len(candidates)]
            r_i += 1
            used.add(repl["title"])
            it["title"], it["location"], it["cost"] = repl["title"], repl["location"], repl["cost"]
            it["status"] = "changed"
            it["reason"] = "Swapped from an outdoor plan to an indoor one because of the weather."
            swapped.append(repl["title"])
    if swapped:
        notes.append(
            f"Bad weather is forecast in {profile['display']}, so outdoor activities were swapped for indoor "
            f"alternatives ({', '.join(swapped)}) to keep the day usable rather than cancelling it."
        )
    else:
        notes.append(
            f"Bad weather is forecast in {profile['display']}, but there were no exposed outdoor activities left "
            f"to swap \u2014 the plan already suits indoor conditions."
        )


def _apply_traffic(items: List[Dict], profile: Dict, desc: str, notes: List[str]) -> None:
    # Insert extra buffer on the busiest day by nudging the first activity/food
    # item of each day forward; recompute_schedule then cascades the rest.
    days = sorted(set(i["day"] for i in items))
    touched = False
    for day in days:
        day_items = sorted([i for i in items if i["day"] == day], key=lambda i: i["minutes"])
        for it in day_items:
            if it["type"] in ("activity", "food"):
                it["minutes"] += 30
                it["time"] = minutes_to_time(it["minutes"])
                it["status"] = "changed"
                it["reason"] = "Start time pushed to add a traffic buffer."
                touched = True
                break
    if touched:
        notes.append(
            f"Severe traffic congestion is reported around {profile['display']}. A 30-minute buffer was added "
            f"before the next stop each day, and later events were shifted so nothing runs back-to-back."
        )


def _apply_closure(items: List[Dict], profile: Dict, desc: str, notes: List[str]) -> None:
    used = _used_titles(items)
    replaced = []
    # Try to match a specific activity named in the description first.
    target = None
    for it in items:
        if it["type"] == "activity" and it["title"].split(" (")[0].lower() in desc:
            target = it
            break
    candidates = [a for a in items if a["type"] == "activity"] if target is None else [target]
    for it in candidates:
        alt = next((a for a in profile["activities"] if a["title"] not in used), None)
        if not alt:
            continue
        used.add(alt["title"])
        old_title = it["title"]
        it["title"], it["location"], it["cost"] = alt["title"], alt["location"], alt["cost"]
        it["status"] = "changed"
        it["reason"] = f"Replaced \u201c{old_title}\u201d, which was closed or overbooked."
        replaced.append((old_title, alt["title"]))
        break  # one closure -> one swap, unless a specific match narrowed it further
    if replaced:
        old_t, new_t = replaced[0]
        notes.append(
            f"\u201c{old_t}\u201d in {profile['display']} is closed or overbooked, so it was swapped for "
            f"\u201c{new_t}\u201d, a nearby alternative that still fits the remaining schedule."
        )
    else:
        notes.append(
            f"An attraction in {profile['display']} is closed or overbooked, but no unused alternative was "
            f"available \u2014 consider adding a custom replacement through chat."
        )


def replan_items(dest_key: str, profile: Dict, description: str, current_items: List[Dict]) -> Tuple[List[Dict], str, List[str]]:
    """Applies every disruption type mentioned in `description` on top of the
    CURRENT itinerary (which may already carry earlier disruptions), then
    recomputes the whole schedule so timing stays chronological and gap-free."""
    desc = (description or "").lower()
    items = [dict(i) for i in current_items]  # shallow clone, preserves ids/history
    notes: List[str] = []
    matched_any = False

    if any(k in desc for k in ["flight", "delay", "late"]):
        _apply_flight_delay(items, profile, desc, notes)
        matched_any = True
    if any(k in desc for k in ["rain", "weather", "storm", "flood", "snow"]):
        _apply_weather(items, profile, desc, notes)
        matched_any = True
    if any(k in desc for k in ["traffic", "congestion", "jam"]):
        _apply_traffic(items, profile, desc, notes)
        matched_any = True
    if any(k in desc for k in ["closed", "overbook", "unavailable", "cancel", "shut"]):
        _apply_closure(items, profile, desc, notes)
        matched_any = True

    if not matched_any:
        notes.append(
            f"We reviewed your {profile['display']} itinerary against: \u201c{description}\u201d. "
            f"It doesn't match a known disruption pattern (flight delay, weather, traffic, closure), "
            f"so no structural changes were made \u2014 try describing it differently or use chat to edit directly."
        )

    shifted = recompute_schedule(items)
    if shifted and matched_any:
        notes.append(
            f"{len(shifted)} later item(s) were automatically retimed to preserve a realistic gap between stops "
            f"and avoid any overlap."
        )

    items = sort_items(items)
    explanation = " ".join(notes)
    changed_ids = [i["id"] for i in items if i["status"] != "on-time"]
    return items, explanation, changed_ids


# =========================================================
#  BUDGET
# =========================================================
def budget_breakdown(items: List[Dict], budget: float) -> Dict:
    total_cost = sum(i["cost"] for i in items)
    remaining = budget - total_cost
    pct = round((total_cost / budget) * 100, 1) if budget else 0
    return {
        "total_budget": round(budget),
        "estimated_cost": round(total_cost),
        "remaining_budget": round(remaining),
        "usage_percent": pct,
        "over_budget": total_cost > budget,
        "transport": sum(i["cost"] for i in items if i["type"] == "transport"),
        "accommodation": sum(i["cost"] for i in items if i["type"] == "hotel"),
        "activities_and_food": sum(i["cost"] for i in items if i["type"] in ("activity", "food")),
    }


# =========================================================
#  CONTEXT-AWARE CHAT ASSISTANT
# =========================================================
def _find_item(items: List[Dict], phrase: str) -> Optional[Dict]:
    phrase = phrase.strip().lower()
    if not phrase:
        return None
    best, best_score = None, 0
    for it in items:
        title = it["title"].lower()
        score = 0
        if phrase in title or title in phrase:
            score = len(phrase)
        else:
            shared = set(phrase.split()) & set(title.split())
            score = len(shared)
        if score > best_score:
            best, best_score = it, score
    return best if best_score > 0 else None


def _pool_lookup(profile: Dict, phrase: str) -> Optional[Dict]:
    phrase = phrase.strip().lower()
    for pool in (profile["activities"], profile["food"]):
        for a in pool:
            if phrase in a["title"].lower() or set(phrase.split()) & set(a["title"].lower().split()):
                return a
    return None


def _next_free_slot(items: List[Dict]) -> Tuple[int, int]:
    if not items:
        return 1, time_to_minutes("9:00 AM")
    last_day = max(i["day"] for i in items)
    day_items = sorted([i for i in items if i["day"] == last_day], key=lambda i: i["minutes"])
    if day_items and day_items[-1]["type"] == "transport":
        # Don't schedule anything after the departure flight; slot in right
        # before it and let recompute_schedule push the flight later instead.
        prior = day_items[-2] if len(day_items) > 1 else None
        start = (prior["minutes"] + prior.get("duration", 60) + DEFAULT_BUFFER_MIN) if prior else day_items[0]["minutes"]
        return last_day, start
    end = max(i["minutes"] + i.get("duration", 60) for i in day_items)
    return last_day, end + DEFAULT_BUFFER_MIN


def _reason_for_item(item: Dict, profile: Dict, preferences: List[str]) -> str:
    if item.get("reason"):
        return item["reason"]
    match = next((a for a in profile["activities"] + profile["food"] if a["title"] == item["title"]), None)
    tags = set(match.get("tags", [])) if match else set()
    pref_hit = tags & set(preferences)
    if pref_hit:
        return f"It was chosen because it matches your selected preference(s): {', '.join(pref_hit)}."
    if item["type"] == "food":
        return "It's scheduled to keep meals evenly spaced between activities."
    if item["type"] == "hotel":
        return "This is your base for the trip, booked to cover every night you're in town."
    if item["type"] == "transport":
        return "This is your inbound/outbound transport, anchoring the rest of the day's timing."
    return "It rounds out a balanced day without overloading the schedule."


PACKING_BASE = ["Phone charger & power bank", "Copy of ID / travel documents", "Reusable water bottle", "Basic first-aid kit"]
PACKING_BY_TAG = {
    "beaches": ["Swimwear", "Sunscreen (SPF 50+)", "Flip-flops", "Quick-dry towel"],
    "adventure": ["Sturdy closed-toe shoes", "Moisture-wicking clothing", "Small daypack"],
    "nature": ["Comfortable walking shoes", "Light rain jacket", "Insect repellent"],
    "history": ["Modest clothing for religious/heritage sites", "Comfortable walking shoes"],
    "museums": ["Comfortable walking shoes"],
    "nightlife": ["An evening outfit", "Portable phone charger"],
    "relaxed": ["A good book", "Sunglasses"],
}


def handle_chat(session: Dict, message: str) -> Dict:
    dest_key, profile = resolve_destination(session["destination"])
    items = session["items"]
    preferences = session.get("preferences", [])
    budget = session["budget"]
    msg = message.strip()
    low = msg.lower()
    suggested = set(session.setdefault("suggested_titles", []))
    modified = False
    reply = ""

    # ---- Conversational itinerary edits ----
    m = re.search(r"(?:replace|swap)\s+(.+?)\s+(?:with|for)\s+(.+)", low)
    if m and not modified:
        target = _find_item(items, m.group(1))
        alt = _pool_lookup(profile, m.group(2))
        if target and alt:
            old_title = target["title"]
            target["title"], target["location"], target["cost"] = alt["title"], alt["location"], alt["cost"]
            target["status"] = "changed"
            target["reason"] = f"Swapped by request, replacing \u201c{old_title}\u201d."
            modified = True
            reply = f"Done \u2014 swapped \u201c{old_title}\u201d for \u201c{alt['title']}\u201d on day {target['day']}."
        elif target and not alt:
            reply = f"I found \u201c{target['title']}\u201d in your itinerary, but couldn't match \u201c{m.group(2)}\u201d to anything I know in {profile['display']}. Try a different name."
        else:
            reply = f"I couldn't find \u201c{m.group(1)}\u201d in your current itinerary to swap out."

    if not modified and not reply and re.search(r"^remove\b|delete\b", low):
        phrase = re.sub(r"^(remove|delete)\s+", "", low).strip()
        target = _find_item(items, phrase)
        if target:
            items[:] = [i for i in items if i["id"] != target["id"]]
            modified = True
            reply = f"Removed \u201c{target['title']}\u201d from day {target['day']}. Remaining events keep their timing."
        else:
            reply = "I couldn't match that to anything currently in your itinerary."

    if not modified and not reply and low.startswith("add "):
        phrase = low[4:].strip()
        alt = _pool_lookup(profile, phrase)
        if alt and alt["title"] not in _used_titles(items):
            day, start = _next_free_slot(items)
            new_item = mk(dest_key, profile, minutes_to_time(start), day, alt["title"], alt["location"],
                          "activity" if alt in profile["activities"] else "food", alt["cost"], status="changed")
            new_item["reason"] = "Added by request through chat."
            items.append(new_item)
            recompute_schedule(items)
            modified = True
            reply = f"Added \u201c{alt['title']}\u201d on day {day} at {new_item['time']}."
        elif alt:
            reply = f"\u201c{alt['title']}\u201d is already in your itinerary."
        else:
            reply = f"I couldn't find anything matching \u201c{phrase}\u201d for {profile['display']}."

    m2 = re.search(r"change (?:the )?time of\s+(.+?)\s+to\s+(\d{1,2}:\d{2}\s*[ap]m)", low)
    if not modified and not reply and m2:
        target = _find_item(items, m2.group(1))
        if target:
            target["minutes"] = time_to_minutes(m2.group(2))
            target["time"] = minutes_to_time(target["minutes"])
            target["status"] = "changed"
            target["reason"] = "Retimed by request through chat."
            recompute_schedule(items)
            modified = True
            reply = f"Moved \u201c{target['title']}\u201d to {target['time']} and adjusted anything that would have overlapped."
        else:
            reply = f"I couldn't find \u201c{m2.group(1)}\u201d in your itinerary to retime."

    if modified:
        items[:] = sort_items(items)
        session["history"].append([dict(i) for i in session["items"]])
        session["history"] = session["history"][-MAX_UNDO_HISTORY:]
        session["items"] = items
        session["chat_log"].append({"role": "user", "content": msg})
        session["chat_log"].append({"role": "assistant", "content": reply})
        return {"reply": reply, "modified": True}

    # ---- Q&A intents (read-only) ----
    if any(k in low for k in ["budget", "afford", "how much", "cost so far", "over budget"]):
        b = budget_breakdown(items, budget)
        if b["over_budget"]:
            reply = (f"You're currently over budget: estimated cost is \u20b9{b['estimated_cost']:,} against a "
                      f"\u20b9{b['total_budget']:,} budget \u2014 that's \u20b9{-b['remaining_budget']:,} over "
                      f"({b['usage_percent']}% used). I can suggest cheaper swaps if you'd like.")
        else:
            reply = (f"You've used {b['usage_percent']}% of your budget: \u20b9{b['estimated_cost']:,} spent, "
                      f"\u20b9{b['remaining_budget']:,} remaining out of \u20b9{b['total_budget']:,}.")

    elif "cheap" in low or "cheaper" in low:
        swappable = [i for i in items if i["type"] in ("activity", "food")]
        priciest = max(swappable, key=lambda i: i["cost"], default=None)
        if priciest:
            pool = profile["activities"] if priciest["type"] == "activity" else profile["food"]
            cheaper_options = sorted(
                [a for a in pool if a["cost"] < priciest["cost"] and a["title"] not in suggested],
                key=lambda a: a["cost"],
            )
            if cheaper_options:
                opt = cheaper_options[0]
                suggested.add(opt["title"])
                reply = (f"Your priciest item right now is \u201c{priciest['title']}\u201d at \u20b9{priciest['cost']:,}. "
                          f"A cheaper alternative is \u201c{opt['title']}\u201d at \u20b9{opt['cost']:,} \u2014 "
                          f"say \"replace {priciest['title']} with {opt['title']}\" to swap it in.")
            else:
                reply = "I don't have a cheaper alternative left that I haven't already suggested \u2014 your current picks are close to the best value available."
        else:
            reply = "Your itinerary is empty, so there's nothing to compare costs on yet."

    elif "pack" in low:
        tags_present = set()
        for it in items:
            match = next((a for a in profile["activities"] if a["title"] == it["title"]), None)
            if match:
                tags_present |= set(match.get("tags", []))
        items_list = list(PACKING_BASE)
        for tag in tags_present:
            items_list += PACKING_BY_TAG.get(tag, [])
        items_list = list(dict.fromkeys(items_list))  # de-dupe, preserve order
        reply = f"Based on your {profile['display']} itinerary, pack: " + "; ".join(items_list) + "."

    elif any(k in low for k in ["rain", "weather", "if it rains", "indoor"]):
        outdoor_tags = {"beaches", "nature", "adventure"}
        def _is_indoor(a):
            tags = set(a.get("tags", []))
            return bool(tags & {"museums", "history", "relaxed"}) and not (tags & outdoor_tags)
        indoor = [a for a in profile["activities"] if _is_indoor(a) and a["title"] not in suggested]
        if not indoor:
            suggested.clear()
            indoor = [a for a in profile["activities"] if _is_indoor(a)]
        picks = indoor[:3]
        for p in picks:
            suggested.add(p["title"])
        if picks:
            reply = "If the weather turns, good indoor options are: " + ", ".join(f"{p['title']} ({p['location']})" for p in picks) + "."
        else:
            reply = f"{profile['display']} doesn't have strong indoor alternatives in my data \u2014 I'd suggest checking local museums or malls on the day."

    elif any(k in low for k in ["restaurant", "eat", "food near", "dinner", "lunch nearby"]):
        options = [f for f in profile["food"] if f["title"] not in suggested]
        if not options:
            suggested.clear()
            options = profile["food"]
        picks = options[:3]
        for p in picks:
            suggested.add(p["title"])
        reply = "Nearby food options worth trying: " + ", ".join(f"{p['title']} in {p['location']} (\u20b9{p['cost']:,})" for p in picks) + "."

    elif low.startswith("why") or "why is" in low or "why was" in low or "explain" in low:
        candidate_phrase = re.sub(r"^(why (is|was|did)?|explain)\s*", "", low).strip(" ?")
        target = _find_item(items, candidate_phrase) or (items[0] if items else None)
        if target:
            reply = f"\u201c{target['title']}\u201d is scheduled at {target['time']} on day {target['day']}. " + _reason_for_item(target, profile, preferences)
        else:
            reply = "Your itinerary is empty right now, so there's nothing to explain yet \u2014 generate a trip first."

    elif any(k in low for k in ["recommend", "suggest", "alternative", "other attraction", "what else"]):
        used = _used_titles(items)
        pool = rank_pool(profile["activities"], preferences)
        options = [a for a in pool if a["title"] not in used and a["title"] not in suggested]
        if not options:
            suggested.clear()
            options = [a for a in pool if a["title"] not in used]
        picks = options[:3]
        for p in picks:
            suggested.add(p["title"])
        if picks:
            reply = f"A few attractions in {profile['display']} you haven't added yet: " + ", ".join(f"{p['title']} ({p['location']})" for p in picks) + "."
        else:
            reply = f"You've already got most of what {profile['display']} has to offer in this itinerary \u2014 nice and thorough!"

    elif any(k in low for k in ["best time", "when to visit", "season"]):
        reply = f"The best time to visit {profile['display']} is {profile['best_time']}."

    elif any(k in low for k in ["currency", "money", "cash", "pay"]):
        reply = profile["currency_note"]

    elif any(k in low for k in ["tip", "advice", "local", "should i know"]):
        reply = profile["local_tip"]

    elif any(k in low for k in ["hello", "hi", "hey"]):
        reply = f"Hey! I'm tracking your {len(items)}-stop {profile['display']} itinerary across {session['days']} day(s). Ask me about budget, restaurants, packing, or say \"remove/add/replace ...\" to edit it directly."

    else:
        reply = (f"I'm using your current {profile['display']} itinerary ({len(items)} stops across {session['days']} "
                  f"day(s)) as context. I can suggest alternative attractions or restaurants, explain why something's "
                  f"scheduled, find cheaper options, answer budget questions, recommend packing items, suggest "
                  f"weather backups, or edit the plan if you say things like \"remove the museum visit\" or "
                  f"\"replace X with Y\". What would you like to know?")

    session["suggested_titles"] = list(suggested)
    session["chat_log"].append({"role": "user", "content": msg})
    session["chat_log"].append({"role": "assistant", "content": reply})
    session["chat_log"] = session["chat_log"][-40:]
    return {"reply": reply, "modified": False}


# =========================================================
#  RESPONSE SHAPING
# =========================================================
def serialize_session(session: Dict) -> Dict:
    items = sort_items(session["items"])
    return {
        "session_id": session["session_id"],
        "items": items,
        "destination": session["destination_display"],
        "days": session["days"],
        "budget": budget_breakdown(items, session["budget"]),
        "can_undo": len(session["history"]) > 0,
    }


# =========================================================
#  ENDPOINTS
# =========================================================
@app.post("/api/generate")
async def generate_itinerary(setup: TripSetup):
    dest_key, profile = resolve_destination(setup.destination)
    items = build_items(dest_key, profile, setup.days, setup.preferences)
    total_cost = sum(item["cost"] for item in items)

    if total_cost > setup.budget:
        transport_total = sum(i["cost"] for i in items if i["type"] == "transport")
        hotel_total = sum(i["cost"] for i in items if i["type"] == "hotel")
        other_total = total_cost - transport_total - hotel_total
        shortfall = total_cost - setup.budget
        recommended = round(total_cost * 1.1)
        return {
            "error": (
                f"Insufficient budget for a {setup.days}-day trip to {profile['display']}. "
                f"Estimated cost is \u20b9{total_cost:,}, which is \u20b9{shortfall:,.0f} more than your budget of "
                f"\u20b9{setup.budget:,.0f}. Please raise your budget to at least \u20b9{total_cost:,} "
                f"(\u20b9{recommended:,} recommended to leave a buffer), or reduce the trip length / preferences."
            ),
            "required_minimum": total_cost,
            "recommended_budget": recommended,
            "shortfall": round(shortfall),
            "breakdown": {
                "transport": transport_total,
                "accommodation": hotel_total,
                "activities_and_food": other_total,
            },
        }

    session_id = uuid.uuid4().hex
    session = {
        "session_id": session_id,
        "destination": setup.destination,
        "destination_display": profile["display"],
        "days": setup.days,
        "budget": setup.budget,
        "preferences": setup.preferences,
        "notes": setup.notes,
        "items": items,
        "original_items": [dict(i) for i in items],
        "history": [],
        "chat_log": [],
        "suggested_titles": [],
        "disruption_log": [],
        "created_at": datetime.utcnow().isoformat(),
    }
    save_session(session_id, session)
    return serialize_session(session)


@app.post("/api/replan")
async def replan(req: DisruptionRequest):
    session = get_session(req.session_id)
    dest_key, profile = resolve_destination(session["destination"])

    session["history"].append([dict(i) for i in session["items"]])
    session["history"] = session["history"][-MAX_UNDO_HISTORY:]

    items, explanation, changed_ids = replan_items(dest_key, profile, req.description, session["items"])
    session["items"] = items
    session["disruption_log"].append({"description": req.description, "explanation": explanation,
                                       "at": datetime.utcnow().isoformat()})
    save_session(req.session_id, session)

    result = serialize_session(session)
    result["explanation"] = explanation
    result["changed_ids"] = changed_ids
    return result


@app.post("/api/chat")
async def chat(req: ChatRequest):
    session = get_session(req.session_id)
    outcome = handle_chat(session, req.message)
    save_session(req.session_id, session)
    result = serialize_session(session)
    result["reply"] = outcome["reply"]
    result["modified"] = outcome["modified"]
    return result


@app.post("/api/undo")
async def undo(req: SessionRequest):
    session = get_session(req.session_id)
    if not session["history"]:
        raise HTTPException(status_code=400, detail="Nothing to undo.")
    session["items"] = session["history"].pop()
    save_session(req.session_id, session)
    result = serialize_session(session)
    result["explanation"] = "Reverted the itinerary to its previous state."
    return result


@app.post("/api/reset")
async def reset(req: SessionRequest):
    session = get_session(req.session_id)
    session["items"] = [dict(i) for i in session["original_items"]]
    session["history"] = []
    session["disruption_log"] = []
    save_session(req.session_id, session)
    result = serialize_session(session)
    result["explanation"] = "Itinerary reset to the originally generated plan."
    return result


@app.post("/api/reorder")
async def reorder(req: ReorderRequest):
    session = get_session(req.session_id)
    items = session["items"]
    day_items = {i["id"]: i for i in items if i["day"] == req.day}
    if set(req.ordered_ids) != set(day_items.keys()):
        raise HTTPException(status_code=400, detail="ordered_ids must exactly match the items on that day.")

    session["history"].append([dict(i) for i in items])
    session["history"] = session["history"][-MAX_UNDO_HISTORY:]

    start = min(i["minutes"] for i in day_items.values())
    cursor = start
    for iid in req.ordered_ids:
        it = day_items[iid]
        it["minutes"] = cursor
        it["time"] = minutes_to_time(cursor)
        it["status"] = "changed"
        it["reason"] = "Reordered by drag-and-drop."
        cursor += it.get("duration", 60) + DEFAULT_BUFFER_MIN

    recompute_schedule(items)
    session["items"] = sort_items(items)
    save_session(req.session_id, session)
    result = serialize_session(session)
    result["explanation"] = f"Day {req.day} was reordered and retimed to stay chronological."
    return result


@app.get("/api/session/{session_id}")
async def get_session_endpoint(session_id: str):
    session = get_session(session_id)
    return serialize_session(session)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("TRAVELGPT_HOST", "0.0.0.0"),
                port=int(os.environ.get("TRAVELGPT_PORT", "8000")))