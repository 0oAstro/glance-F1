from fastapi import APIRouter
from fastapi_cache import FastAPICache
import httpx
from datetime import datetime, timedelta
import hashlib
import json
import re

from .helpers.functions import country_to_code, get_next_race_end, format_team_name
from .helpers.global_vars import NEXT_RACE_API_URL, country_correction_map, default_expire
from .helpers.time_functions import MT, UTC

router = APIRouter()
F1_RESULTS_URL = "https://www.formula1.com/en/results/{season}/drivers"


def make_signature(results):
    return hashlib.md5(json.dumps(results, sort_keys=True).encode()).hexdigest()


def strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(value.replace("\xa0", " ").split())


def clean_team(team: str) -> str:
    team = re.sub(r"-(Mercedes|Ferrari|Red Bull-Ford|Honda)$", "", team).strip()
    replacements = {
        "Red Bull Racing": "Red Bull",
        "Racing Bulls": "RB",
        "Atlassian Williams Mercedes": "Williams",
        "Haas F1 Team": "Haas",
        "Aston Martin Aramco Honda": "Aston Martin",
    }
    return replacements.get(team, format_team_name(team))


def nationality_to_country(nationality: str) -> str:
    return {
        "ITA": "Italy", "GBR": "Great Britain", "MON": "Monaco", "AUS": "Australia",
        "NED": "Netherlands", "FRA": "France", "NZL": "New Zealand", "ARG": "Argentina",
        "ESP": "Spain", "BRA": "Brazil", "GER": "Germany", "FIN": "Finland",
        "MEX": "Mexico", "CAN": "Canada", "THA": "Thailand",
    }.get(nationality, nationality)


async def fetch_formula1_drivers(season: int):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = await client.get(F1_RESULTS_URL.format(season=season), timeout=60)
        response.raise_for_status()
        html = response.text

    table = re.search(r'<tbody[^>]*class="[^"]*Table-module_tbody[^>]*>(.*?)</tbody>', html, re.S)
    if not table:
        raise ValueError("Formula1 drivers standings table not found")

    results = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', table.group(1), re.S):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
        if len(cells) < 5:
            continue
        position = strip_tags(cells[0])
        driver_text = strip_tags(cells[1])
        nationality = strip_tags(cells[2])
        team = strip_tags(cells[3])
        points = strip_tags(cells[4])
        parts = driver_text.split()
        surname = parts[1] if len(parts) >= 2 else driver_text
        country = nationality_to_country(nationality)
        results.append({
            "surname": surname,
            "position": int(position) if position.isdigit() else position,
            "points": int(points) if points.isdigit() else points,
            "teamId": clean_team(team),
            "country": country,
            "flag": country_to_code(country),
        })
    if not results:
        raise ValueError("Formula1 drivers standings table was empty")
    return results


async def fetch_f1api_drivers():
    async with httpx.AsyncClient() as client:
        response = await client.get("https://f1api.dev/api/current/drivers-championship", timeout=60)
        response.raise_for_status()
        data = response.json()

    results = []
    for entry in data.get("drivers_championship", []):
        driver = entry.get("driver", {})
        team = entry.get("team", {})
        country = driver.get("nationality", "")
        if country in country_correction_map:
            country = country_correction_map[country]
        results.append({
            "surname": driver.get("surname"),
            "position": entry.get("position"),
            "points": entry.get("points"),
            "teamId": format_team_name(team.get("teamId")),
            "country": country,
            "flag": country_to_code(country),
        })
    return data.get("season"), results


@router.get("/", summary="Fetch current drivers championship")
async def get_drivers_championship():
    cache = FastAPICache.get_backend()
    cache_key = "drivers_championship"

    cached = await cache.get(cache_key)
    if cached:
        return cached

    season = datetime.now(MT).year
    source = "formula1.com"
    try:
        results = await fetch_formula1_drivers(season)
    except Exception:
        source = "f1api.dev-fallback"
        season, results = await fetch_f1api_drivers()

    now = datetime.now(MT)
    race_dt = await get_next_race_end()
    expire = default_expire
    expiry_dt = now + timedelta(seconds=default_expire)
    if race_dt:
        if race_dt > now:
            expire = min(default_expire, max(60, int((race_dt - now).total_seconds())))
            expiry_dt = now + timedelta(seconds=expire)
        elif now < race_dt + timedelta(seconds=default_expire):
            expiry_dt = race_dt + timedelta(seconds=default_expire)
            expire = max(60, int((expiry_dt - now).total_seconds()))

    response_data = {
        "season": season,
        "source": source,
        "cache_expires": expiry_dt.isoformat(),
        "drivers": results,
        "result_signature": make_signature(results),
    }

    await cache.set(cache_key, response_data, expire=expire)
    return response_data
