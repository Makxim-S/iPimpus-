import hashlib
import json
import os
import time
from datetime import datetime
from typing import Any

import aiohttp
from dotenv import load_dotenv

from config import CHILDREN


load_dotenv()


AUTH_URL = "http://127.0.0.1:8099"
BASE_URL = "https://kidsmanagement-pa.clients6.google.com/kidsmanagement/v1"
ORIGIN = "https://familylink.google.com"
GOOG_API_KEY = os.environ.get("GOOGLE_API_KEY")


def _cookie_data(cookies: list[dict[str, Any]]) -> dict[str, str]:
    return {
        cookie["name"]: cookie["value"].strip('"')
        for cookie in cookies
        if isinstance(cookie, dict)
        and cookie.get("name")
        and cookie.get("value") is not None
    }


def _sapisidhash(cookies: dict[str, str]) -> str:
    sapisid = cookies.get("SAPISID")
    if not sapisid:
        raise RuntimeError("SAPISID cookie not found")
    timestamp = int(time.time())
    digest = hashlib.sha1(
        f"{timestamp} {sapisid} {ORIGIN}".encode("utf-8")
    ).hexdigest()
    return f"{timestamp}_{digest}"


async def _load_cookies() -> list[dict[str, Any]]:
    api_key = os.environ.get("FAMILYLINK_API_KEY")
    if not api_key:
        raise RuntimeError("FAMILYLINK_API_KEY is not configured")
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{AUTH_URL}/api/cookies",
            headers={"X-API-Key": api_key},
        ) as response:
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"Family Link auth returned HTTP {response.status}: {body[:200]}"
                )
            data = await response.json()
    cookies = data.get("cookies", []) if isinstance(data, dict) else data
    if not cookies:
        raise RuntimeError("Family Link cookies are empty")
    return cookies


async def add_time_bonus(child_key: str, minutes: int) -> dict[str, Any]:
    if not GOOG_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not configured")
    if child_key not in CHILDREN:
        raise ValueError(f"Unknown child: {child_key}")
    if not 1 <= minutes <= 1440:
        raise ValueError("Bonus must be between 1 and 1440 minutes")

    child = CHILDREN[child_key]
    cookies = _cookie_data(await _load_cookies())
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Origin": ORIGIN,
        "Content-Type": "application/json+protobuf",
        "X-Goog-Api-Key": GOOG_API_KEY,
        "Authorization": f"SAPISIDHASH {_sapisidhash(cookies)}",
        "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
    }
    seconds = minutes * 60
    override = [
        None,
        None,
        10,
        child["family_link_device_id"],
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        [[str(seconds), 0]],
    ]
    payload = json.dumps([None, child["family_link_child_id"], [override], [1]])
    url = f"{BASE_URL}/people/{child['family_link_child_id']}/timeLimitOverrides:batchCreate"

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        async with session.post(url, data=payload) as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    f"Google Family Link returned HTTP {response.status}: {body[:500]}"
                )
            return {
                "child_key": child_key,
                "minutes": minutes,
                "status": response.status,
                "response": body[:2000],
            }


async def get_child_location(child_key: str, refresh: bool = False) -> dict[str, Any] | None:
    if not GOOG_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not configured")
    if child_key not in CHILDREN:
        raise ValueError(f"Unknown child: {child_key}")

    child = CHILDREN[child_key]
    cookies = _cookie_data(await _load_cookies())
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Origin": ORIGIN,
        "Content-Type": "application/json+protobuf",
        "X-Goog-Api-Key": GOOG_API_KEY,
        "Authorization": f"SAPISIDHASH {_sapisidhash(cookies)}",
        "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
    }
    params = [
        ("locationRefreshMode", "2" if refresh else "1"),
        ("supportedConsents", "SUPERVISED_LOCATION_SHARING"),
    ]
    url = f"{BASE_URL}/families/mine/location/{child['family_link_child_id']}"

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        async with session.get(url, params=params) as response:
            if response.status == 404:
                return None
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    f"Google Family Link location returned HTTP {response.status}: {body[:500]}"
                )
            data = json.loads(body)

    if not isinstance(data, list) or len(data) < 2:
        return None
    child_data = data[1]
    location = child_data[2] if isinstance(child_data, list) and len(child_data) > 2 else None
    if not isinstance(location, list) or len(location) < 2:
        return None
    coords = location[0]
    if not isinstance(coords, list) or len(coords) < 2:
        return None

    timestamp_ms = int(location[1]) if location[1] else None
    place = location[4] if len(location) > 4 else None
    battery = location[8] if len(location) > 8 else None
    return {
        "child_key": child_key,
        "child_name": child["name"],
        "latitude": float(coords[0]),
        "longitude": float(coords[1]),
        "accuracy": int(location[2]) if len(location) > 2 and location[2] else None,
        "timestamp": timestamp_ms,
        "timestamp_iso": datetime.fromtimestamp(timestamp_ms / 1000).astimezone().isoformat() if timestamp_ms else None,
        "place_name": place[1] if isinstance(place, list) and len(place) > 1 else None,
        "place_address": place[2] if isinstance(place, list) and len(place) > 2 else None,
        "source_device_id": location[6] if len(location) > 6 else None,
        "battery_level": int(battery[0]) if isinstance(battery, list) and battery and battery[0] is not None else None,
        "refreshed": refresh,
    }
