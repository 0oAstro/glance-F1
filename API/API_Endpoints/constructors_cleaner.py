from fastapi import APIRouter
from fastapi_cache import FastAPICache
import httpx
from datetime import datetime, timedelta
import hashlib
import json
import re

from .helpers.functions import country_to_code, get_next_race_end
from .helpers.global_vars import default_expire
from .helpers.time_functions import MT

router = APIRouter()
F1_RESULTS_URL = "https://www.formula1.com/en/results/{season}/team"


def make_signature(results):
    return hashlib.md5(json.dumps(results, sort_keys=True).encode()).hexdigest()


def strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(value.replace("\xa0", " ").split())


def clean_team(team: str) -> str:
    return {
        "Haas F1 Team": "Haas",
        "Red Bull Racing": "Red Bull",
        "Racing Bulls": "RB",
    }.get(team, team)


async def fetch_formula1_constructors(season: int):
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = await client.get(F1_RESULTS_URL.format(season=season), timeout=60)
        response.raise_for_status()
        html = response.text

    table = re.search(r'<tbody[^>]*class="[^"]*Table-module_tbody[^>]*>(.*?)</tbody>', html, re.S)
    if not table:
        raise ValueError("Formula1 team standings table not found")

    results = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', table.group(1), re.S):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
        if len(cells) < 3:
            continue
        position = strip_tags(cells[0])
        team = clean_team(strip_tags(cells[1]))
        points = strip_tags(cells[2])
        results.append({
            "team": team,
            "position": int(position) if position.isdigit() else position,
            "points": int(points) if points.isdigit() else points,
            "wins": 0,
            "country": "",
            "flag": country_to_code(""),
            "wiki": "",
        })
    if not results:
        raise ValueError("Formula1 team standings table was empty")
    return results


async def fetch_f1api_constructors():
    async with httpx.AsyncClient() as client:
        response = await client.get("https://f1api.dev/api/current/constructors-championship", timeout=60)
        response.raise_for_status()
        data = response.json()

    results = []
    for entry in data.get("constructors_championship", []):
        team = entry.get("team", {})
        team_name = team.get("teamName") or ""
        for word in ["Formula 1", "F1", "Racing", "Team", "Scuderia"]:
            team_name = team_name.replace(word, "").strip()
        country = team.get("country", "")
        results.append({
            "team": team_name,
            "position": entry.get("position"),
            "points": entry.get("points"),
            "wins": entry.get("wins") or 0,
            "country": country,
            "flag": country_to_code(country),
            "wiki": team.get("url"),
        })
    return data.get("season"), results


@router.get("/", summary="Fetch current constructors championship")
async def get_constructors_championship():
    cache = FastAPICache.get_backend()
    cache_key = "constructors_championship"

    cached = await cache.get(cache_key)
    if cached:
        return cached

    season = datetime.now(MT).year
    source = "formula1.com"
    try:
        results = await fetch_formula1_constructors(season)
    except Exception:
        source = "f1api.dev-fallback"
        season, results = await fetch_f1api_constructors()

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
        "constructors": results,
        "result_signature": make_signature(results),
    }

    await cache.set(cache_key, response_data, expire=expire)
    return response_data
