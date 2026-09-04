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


def geocode(name: str) -> dict:
    """Resolve a place name to coordinates. Raises WeatherUnavailable if nothing usable comes back."""
    try:
        r = httpx.get(GEOCODE_URL, params={"name": name, "count": 5, "language": "en"}, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherUnavailable(f"geocoding service unreachable ({exc.__class__.__name__})") from exc

    results = payload.get("results") or []
    if not results:
        raise WeatherUnavailable(f"no location found matching {name!r}")

    top = results[0]
    return {
        "name": top["name"],
        "admin1": top.get("admin1"),
        "country": top.get("country"),
        "latitude": top["latitude"],
        "longitude": top["longitude"],
        "alternatives": [
            f"{r['name']}, {r.get('admin1') or ''} {r.get('country') or ''}".strip(" ,")
            for r in results[1:4]
        ],
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
