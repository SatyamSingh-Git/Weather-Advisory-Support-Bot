"""Open-Meteo access. This module is the only place weather numbers enter the system."""

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "weather_code,cloud_cover,wind_speed_10m,wind_gusts_10m,uv_index,is_day"
)
HOURLY_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "precipitation_probability,weather_code,wind_speed_10m,wind_gusts_10m,uv_index,visibility"
)
DAILY_FIELDS = (
    "precipitation_sum,precipitation_probability_max,wind_gusts_10m_max,uv_index_max,weather_code"
)

TIMEOUT = 10.0


class WeatherUnavailable(Exception):
    """We could not obtain real data. The graph turns this into an honest failure, never a guess."""


def _search(name: str) -> list:
    try:
        r = httpx.get(GEOCODE_URL, params={"name": name, "count": 10, "language": "en"}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("results") or []
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherUnavailable(f"geocoding service unreachable ({exc.__class__.__name__})") from exc


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
        "forecast_days": 3,
        "wind_speed_unit": "kmh",
    }
    try:
        r = httpx.get(FORECAST_URL, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherUnavailable(f"weather service unreachable ({exc.__class__.__name__})") from exc

    if not payload.get("current") or not payload.get("hourly", {}).get("time"):
        raise WeatherUnavailable("weather service returned metadata with no values")
    return payload
