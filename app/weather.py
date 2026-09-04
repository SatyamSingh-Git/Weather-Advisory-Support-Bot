"""Open-Meteo access. This module is the only place weather numbers enter the system."""

import time

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Only what facts.py actually reads. Open-Meteo weights its rate limit by variables x days, and a
# deployed instance shares an egress IP with every other tenant, so an unused field is not free.
CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,"
    "wind_speed_10m,wind_gusts_10m,uv_index,is_day"
)
HOURLY_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "precipitation_probability,weather_code,wind_speed_10m,wind_gusts_10m,uv_index,visibility"
)
DAILY_FIELDS = "precipitation_sum"

TIMEOUT = 12.0
RETRY_STATUSES = {429, 500, 502, 503, 504}
HEADERS = {"User-Agent": "weather-advisory-bot (github.com/SatyamSingh-Git/Weather-Advisory-Support-Bot)"}


class WeatherUnavailable(Exception):
    """We could not obtain real data. The graph turns this into an honest failure, never a guess."""


def _describe(exc: httpx.HTTPStatusError) -> str:
    """Open-Meteo answers errors with {"error": true, "reason": "..."} - report what it said."""
    response = exc.response
    try:
        reason = response.json().get("reason")
    except ValueError:
        reason = (response.text or "").strip()[:160]
    return f"HTTP {response.status_code}" + (f", {reason}" if reason else "")


def _get(url: str, params: dict) -> dict:
    """One request, retried once on a rate limit or a transient upstream error.

    Shared hosting sits behind shared egress IPs, so a free-tier rate limit is a thing that
    happens to a deployed instance and never to a laptop. Worth one retry before failing.
    """
    for attempt in (1, 2):
        try:
            response = httpx.get(url, params=params, timeout=TIMEOUT, headers=HEADERS)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in RETRY_STATUSES and attempt == 1:
                time.sleep(1.5)
                continue
            raise WeatherUnavailable(f"weather service said {_describe(exc)}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            if attempt == 1:
                time.sleep(0.8)
                continue
            raise WeatherUnavailable(f"weather service unreachable ({exc.__class__.__name__})") from exc
    raise WeatherUnavailable("weather service unreachable")

def _search(name: str) -> list:
    return _get(GEOCODE_URL, {"name": name, "count": 10, "language": "en"}).get("results") or []


def _attempts(name: str):
    """The geocoder matches a single place name, so "Alipur, Delhi" finds nothing.

    Fall back to the leading part as the name and keep the rest as a region hint, which is also
    what disambiguates same-named places rather than silently taking the first result.
    """
    yield name, None
    if "," in name:
        head, _, tail = name.partition(",")
        yield head.strip(), tail.strip()
    words = name.replace(",", " ").split()
    if len(words) > 1:
        yield " ".join(words[:-1]), words[-1]


def _pick(results: list, hint: str | None) -> dict:
    """Choose among same-named places using the rest of what the user typed.

    "Alipur, Delhi" matches on region. "Bandra, Mumbai" cannot, because Mumbai is a city and the
    results only carry a state: so resolve the hint too and take the nearest candidate to it.
    Distance is squared degrees, which is crude but only ever used to rank candidates.
    """
    if not hint:
        return results[0]

    needle = hint.casefold()
    for result in results:
        region = f"{result.get('admin1') or ''} {result.get('country') or ''}".casefold()
        if needle in region or (result.get("admin1") and result["admin1"].casefold() in needle):
            return result

    anchors = _search(hint)
    if anchors:
        lat, lon = anchors[0]["latitude"], anchors[0]["longitude"]
        return min(results, key=lambda r: (r["latitude"] - lat) ** 2 + (r["longitude"] - lon) ** 2)
    return results[0]


def geocode(name: str) -> dict:
    """Resolve a place name to coordinates. Raises WeatherUnavailable if nothing usable comes back."""
    for query, hint in _attempts(name):
        results = _search(query)
        if results:
            break
    else:
        raise WeatherUnavailable(f"no location found matching {name!r}")

    top = _pick(results, hint)
    return {
        "name": top["name"],
        "admin1": top.get("admin1"),
        "country": top.get("country"),
        "latitude": top["latitude"],
        "longitude": top["longitude"],
        "matched_on": query if query != name else None,
        "alternatives": [
            f"{r['name']}, {r.get('admin1') or ''} {r.get('country') or ''}".strip(" ,")
            for r in results if r is not top
        ][:3],
    }


def fetch_forecast(latitude: float, longitude: float) -> dict:
    """Fetch current + 48h hourly + 2 day daily. Raises WeatherUnavailable rather than returning partials."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": CURRENT_FIELDS,
        "hourly": HOURLY_FIELDS,
        "daily": DAILY_FIELDS,
        "timezone": "auto",
        "forecast_days": 2,
        "wind_speed_unit": "kmh",
    }
    payload = _get(FORECAST_URL, params)
    if not payload.get("current") or not payload.get("hourly", {}).get("time"):
        raise WeatherUnavailable("weather service returned metadata with no values")
    return payload
