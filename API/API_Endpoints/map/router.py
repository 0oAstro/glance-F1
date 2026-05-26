from datetime import datetime, timedelta
import base64
import hashlib
import html
import json
import re

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi_cache import FastAPICache
import httpx

from .map_generator import generate_track_map_svg
from ..helpers.global_vars import NEXT_RACE_API_URL, default_expire
from ..helpers.time_functions import MT

router = APIRouter()
TRACK_STROKE_WIDTH = "95"


def make_signature(data):
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()


async def get_race_data():
    async with httpx.AsyncClient() as client:
        response = await client.get(NEXT_RACE_API_URL, timeout=60)
        response.raise_for_status()
        return response.json()


def normalize_name(value):
    normalized = str(value or "").casefold().strip()
    return re.sub(r"\s+", " ", normalized)


def historical_event_matches(event, city, country, race_name):
    location_matches = city and normalize_name(event.get("Location")) == normalize_name(city)
    country_matches = country and normalize_name(event.get("Country")) == normalize_name(country)
    event_name_matches = race_name and normalize_name(event.get("EventName")) == normalize_name(race_name)
    return (location_matches and country_matches) or event_name_matches


def cache_expiry(data):
    race = data.get("race", [{}])[0]
    race_dt_str = race.get("schedule", {}).get("race", {}).get("datetime_rfc3339")
    now = datetime.now(MT)
    if not race_dt_str:
        return now + timedelta(seconds=default_expire)

    race_dt = datetime.fromisoformat(race_dt_str).astimezone(MT)
    if now < race_dt:
        return min(race_dt, now + timedelta(seconds=default_expire))
    if now < race_dt + timedelta(seconds=default_expire):
        return race_dt + timedelta(seconds=default_expire)
    return now + timedelta(seconds=default_expire)


def generate_historical_track_map(data):
    race = data.get("race", [{}])[0]
    circuit = race.get("circuit") or {}
    city = circuit.get("city")
    country = circuit.get("country")
    track = circuit.get("circuitName")
    race_name = race.get("raceName")
    current_year = int(data.get("season", datetime.now().year))

    errors = []
    for year in range(current_year - 1, 2017, -1):
        attempts = []
        if race_name:
            attempts.append({"year": year, "race_name": race_name, "track": track, "session_type": "Q"})
        if city and country:
            attempts.append({"year": year, "city": city, "country": country, "track": track, "session_type": "Q"})

        for kwargs in attempts:
            try:
                svg = generate_track_map_svg(**kwargs)
                return svg, year
            except Exception as exc:
                errors.append(f"{year}: {type(exc).__name__}: {exc}")

    raise ValueError("Could not fetch a historical track map. " + " | ".join(errors[-6:]))


def make_dashboard_svg(svg):
    return (
        svg
        .replace("stroke-width: 40;", f"stroke-width: {TRACK_STROKE_WIDTH};")
        .replace(
            "filter: drop-shadow(0 0 40px white), drop-shadow(0 0 70px #e10600);",
            "filter: drop-shadow(0 0 22px rgba(0,0,0,0.65)), drop-shadow(0 0 34px rgba(255,255,255,0.70));",
        )
    )


def make_fallback_svg(data, error):
    race = data.get("race", [{}])[0]
    circuit = race.get("circuit") or {}
    title = html.escape(circuit.get("circuitName") or race.get("raceName") or "Track map")
    detail = html.escape("FastF1 telemetry unavailable")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="170" viewBox="0 0 300 170" role="img" aria-label="{title}">
  <rect width="300" height="170" rx="18" fill="rgba(255,255,255,0.035)"/>
  <path d="M52 104 C65 40 144 28 204 52 C261 75 249 141 176 132 C115 124 99 76 149 66 C185 59 204 82 191 101 C175 125 112 119 86 92" fill="none" stroke="#e10600" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>
  <text x="150" y="142" text-anchor="middle" fill="currentColor" font-family="sans-serif" font-size="15" font-weight="700">{title}</text>
  <text x="150" y="160" text-anchor="middle" fill="currentColor" opacity="0.65" font-family="sans-serif" font-size="11">{detail}</text>
</svg>'''


async def get_map_payload():
    cache_key = "track_map_svg"
    cache = FastAPICache.get_backend()

    try:
        data = await get_race_data()
    except Exception as exc:
        return PlainTextResponse(f"Failed to fetch race info: {exc}", status_code=502)

    signature = make_signature({
        "race": data.get("race"),
        "next_event": data.get("next_event"),
    })
    cached = await cache.get(cache_key)
    now = datetime.now(MT)
    if cached and cached.get("signature") == signature:
        expires_at = cached.get("expires_at")
        if expires_at:
            expires_at = datetime.fromisoformat(expires_at)
        if not expires_at or expires_at > now:
            return cached

    try:
        svg, source_year = generate_historical_track_map(data)
        error = None
    except Exception as exc:
        svg = make_fallback_svg(data, exc)
        source_year = None
        error = str(exc)
    svg = make_dashboard_svg(svg)

    expires_at = cache_expiry(data)
    expire_seconds = max(int((expires_at - now).total_seconds()), 60)
    payload = {
        "svg": svg,
        "svg_base64": base64.b64encode(svg.encode("utf-8")).decode("ascii"),
        "signature": signature,
        "source_year": source_year,
        "error": error,
        "expires_at": expires_at.isoformat(),
    }
    await cache.set(cache_key, payload, expire=expire_seconds)
    return payload


@router.get("/", summary="Fetch next track map")
async def get_dynamic_track_map():
    payload = await get_map_payload()
    if isinstance(payload, PlainTextResponse):
        return payload
    return Response(content=payload["svg"], media_type="image/svg+xml")


@router.get("/json", summary="Fetch next track map as JSON")
async def get_dynamic_track_map_json():
    payload = await get_map_payload()
    if isinstance(payload, PlainTextResponse):
        return payload
    return JSONResponse({
        "svg": html.escape(payload["svg"]),
        "svg_base64": payload["svg_base64"],
        "source_year": payload["source_year"],
        "error": payload.get("error"),
    })
