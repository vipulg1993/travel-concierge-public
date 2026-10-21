#!/usr/bin/env python3
"""Seed travel-concierge-flights and travel-concierge-hotels with mock data.

Puts ~5 flights per destination × 10 destinations = ~50 flight items.
Puts ~5 hotels  per destination × 10 destinations = ~50 hotel items.

Schema:
  flights: PK=destination (string, lowercase)
           other attrs: flight_id, airline, origin, price (int, USD),
                        duration_min (int), stops (int), departure (ISO date)
  hotels:  PK=destination
           other attrs: hotel_id, name, star_rating (int 1-5),
                        price_per_night (int, USD), amenities (list of string)

Idempotent: a repeat run overwrites items with the same (PK, SK) — but there is
no SK, so a repeat would just re-write items with the same destination
collision. We work around that by giving each flight a composite destination
key like "seattle" and putting flight_id as a non-key attribute, then issuing
batched writes keyed on destination which creates one Item per flight_id.

Correction: DynamoDB only accepts one Item per PK (no SK), so we use a GSI-free
simpler schema: each destination gets ONE Item whose "flights" attribute is a
list. The specialist's Query returns that one Item and the LLM reads its list.

For a 300-level demo, this keeps the trace simple (one Query -> one Item).
"""

import logging
import random
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _shared import FLIGHTS_TABLE, HOTELS_TABLE, SESSION  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("seed")

ddb = SESSION.resource("dynamodb")

DESTINATIONS = [
    "seattle",
    "austin",
    "nyc",
    "san francisco",
    "chicago",
    "miami",
    "denver",
    "boston",
    "portland",
    "nashville",
]

AIRLINES = [
    "Alaska", "American", "Delta", "United", "Southwest", "JetBlue", "Spirit"
]
ORIGINS = ["SFO", "LAX", "ORD", "ATL", "DFW", "JFK", "SEA", "BOS", "IAD"]
HOTEL_PREFIXES = ["The", "Grand", "Hotel", "Hilton", "Marriott", "Westin",
                  "Hyatt", "Four Points", "Residence", "Courtyard"]
HOTEL_SUFFIXES = ["Downtown", "Harbor", "Plaza", "Suites", "Place",
                  "Garden", "Marina", "Tower", "House", "Lodge"]
AMENITIES_POOL = ["wifi", "pool", "gym", "parking", "spa", "restaurant",
                  "bar", "pets", "ev-charging", "business-center"]


def _holiday_date() -> str:
    """Return a random departure weighted toward Christmas week."""
    # Weighted so most flights land Dec 22-28, with Thanksgiving + New Year as alternates
    r = random.random()
    if r < 0.6:
        # Christmas week — Dec 22-28, 2026
        return f"2026-12-{random.randint(22,28):02d}T{random.randint(5,21):02d}:00"
    elif r < 0.8:
        # Thanksgiving — Nov 24-30, 2026
        return f"2026-11-{random.randint(24,30):02d}T{random.randint(5,21):02d}:00"
    else:
        # New Year — Dec 29 2026 - Jan 3 2027
        day = random.randint(29, 34)
        if day <= 31:
            return f"2026-12-{day:02d}T{random.randint(5,21):02d}:00"
        else:
            return f"2027-01-{day-31:02d}T{random.randint(5,21):02d}:00"


def _rand_flight(dest: str, idx: int) -> dict:
    base = {
        "seattle": 320, "austin": 280, "nyc": 400, "san francisco": 340,
        "chicago": 310, "miami": 350, "denver": 220, "boston": 390,
        "portland": 300, "nashville": 290,
    }.get(dest, 300)
    # Holiday-season surcharge — looks realistic in demo
    base = int(base * 1.3)
    airline = random.choice(AIRLINES)
    return {
        "flight_id": f"{airline[:2].upper()}{random.randint(100,9999)}",
        "airline": airline,
        "origin": random.choice(ORIGINS),
        "price": base + random.randint(-50, 400),
        "duration_min": random.randint(90, 420),
        "stops": random.choice([0, 0, 0, 1, 1, 2]),
        "departure": _holiday_date(),
    }


def _rand_hotel(dest: str, idx: int) -> dict:
    base = {
        "seattle": 180, "austin": 160, "nyc": 280, "san francisco": 240,
        "chicago": 170, "miami": 200, "denver": 140, "boston": 230,
        "portland": 150, "nashville": 170,
    }.get(dest, 160)
    name = f"{random.choice(HOTEL_PREFIXES)} {dest.title()} {random.choice(HOTEL_SUFFIXES)}"
    # Tighter price variance so each destination has budget AND premium options
    return {
        "hotel_id": f"H{random.randint(1000,99999)}",
        "name": name,
        "star_rating": random.choice([2, 3, 3, 4, 4, 5]),
        "price_per_night": base + random.randint(-40, 200),
        "amenities": random.sample(AMENITIES_POOL, k=random.randint(3, 6)),
    }


def _to_decimal(obj):
    """DynamoDB resource API chokes on floats; convert leaves to Decimal."""
    if isinstance(obj, list):
        return [_to_decimal(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, float):
        return Decimal(str(obj))
    return obj


def seed_flights(items_per_dest: int = 8) -> None:
    table = ddb.Table(FLIGHTS_TABLE)
    count = 0
    for dest in DESTINATIONS:
        flights = [_rand_flight(dest, i) for i in range(items_per_dest)]
        # Each destination -> one Item with a flights list, which the specialist
        # unpacks after Query. The destination string is lowercase (PK).
        item = {"destination": dest, "flights": flights}
        table.put_item(Item=_to_decimal(item))
        count += 1
    log.info("flights: wrote %d destination items", count)


def seed_hotels(items_per_dest: int = 8) -> None:
    table = ddb.Table(HOTELS_TABLE)
    count = 0
    for dest in DESTINATIONS:
        hotels = [_rand_hotel(dest, i) for i in range(items_per_dest)]
        item = {"destination": dest, "hotels": hotels}
        table.put_item(Item=_to_decimal(item))
        count += 1
    log.info("hotels: wrote %d destination items", count)


def main() -> None:
    random.seed(42)  # repeatable
    seed_flights()
    seed_hotels()
    log.info("Seeded 10 destinations with 5 flights + 5 hotels each.")


if __name__ == "__main__":
    main()
