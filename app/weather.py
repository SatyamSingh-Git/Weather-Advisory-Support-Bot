"""Open-Meteo access. This module is the only place weather numbers enter the system."""

import time
from collections.abc import Iterator

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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

# Open-Meteo's free tier meters per IP per day, and shared hosting shares that IP with strangers.
# A forecast does not change between two questions asked a minute apart, so caching is both the
# obvious efficiency and the thing that keeps a demo alive on somebody else's exhausted quota.
FRESH_SECONDS = 15 * 60
STALE_LIMIT_SECONDS = 3 * 60 * 60

_forecast_cache: dict[tuple, tuple[float, dict]] = {}
_geocode_cache: dict[str, list] = {}


class ForecastPayload(BaseModel):
    """What a usable Open-Meteo response must contain.

    The API answers a request with no field list using 200 and a body of metadata with no values,
    which is the documented way to be fooled by it. Validating here means the graph either has real
    readings or takes the honest-failure branch, with no third state where it half-has them.
    """

    model_config = ConfigDict(extra="allow")

    current: dict
    hourly: dict
    daily: dict = Field(default_factory=dict)

    @field_validator("current")
    @classmethod
    def current_has_readings(cls, current: dict) -> dict:
        """A response without these came back from a request that forgot its field list."""
        missing = {"time", "temperature_2m", "wind_speed_10m"} - current.keys()
        if missing:
            raise ValueError(f"missing {sorted(missing)}, was current= given a field list?")
        return current

    @field_validator("hourly")
    @classmethod
    def hourly_has_a_timeline(cls, hourly: dict) -> dict:
        """Equal-length series matter: facts.py indexes them all by the same hour offset."""
        if not hourly.get("time"):
            raise ValueError("no hourly timeline returned")
        lengths = {len(v) for v in hourly.values() if isinstance(v, list)}
        if len(lengths) > 1:
            raise ValueError(f"hourly series have mismatched lengths {sorted(lengths)}")
        return hourly


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
    """Cached for the life of the process: a place does not move."""
    key = name.strip().casefold()
    if key not in _geocode_cache:
        _geocode_cache[key] = _get(GEOCODE_URL, {"name": name, "count": 10, "language": "en"}).get("results") or []
    return _geocode_cache[key]


def _attempts(name: str) -> Iterator[tuple[str, str | None]]:
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
    key = (round(latitude, 2), round(longitude, 2))
    cached = _forecast_cache.get(key)
    if cached and time.time() - cached[0] < FRESH_SECONDS:
        return _with_age(cached)

    try:
        payload = _get(FORECAST_URL, params)
    except WeatherUnavailable:
        # A reading we actually took twenty minutes ago is still a real reading, and saying so with
        # its timestamp is honest. Inventing one would not be. Past the stale limit we fail instead.
        if cached and time.time() - cached[0] < STALE_LIMIT_SECONDS:
            return _with_age(cached)
        raise

    try:
        ForecastPayload(**payload)
    except ValidationError as exc:
        problems = "; ".join(f"{exc.errors()[0]['loc'][0]}: {e['msg']}" for e in exc.errors()[:2])
        raise WeatherUnavailable(f"weather service returned an unusable payload ({problems})") from exc
    _forecast_cache[key] = (time.time(), payload)
    return _with_age(_forecast_cache[key])


def _with_age(entry: tuple[float, dict]) -> dict:
    """Stamp how old the reading is, so everything downstream can say so rather than imply it is now."""
    fetched_at, payload = entry
    payload["_age_seconds"] = int(time.time() - fetched_at)
    return payload
