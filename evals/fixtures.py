"""Synthetic and recorded Open-Meteo payloads.

Live weather does not hold still for a test suite, so the assertions that need a known answer
run against these frozen payloads. The live-API cases assert invariants instead of values.
"""

from datetime import datetime, timedelta

BASE_DAY = "2026-07-15"
HOURS = 72


def _times(base_day: str, count: int) -> list[str]:
    start = datetime.fromisoformat(base_day)
    return [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(count)]


def _uv_curve(peak: float, times: list[str]) -> list[float]:
    out = []
    for stamp in times:
        hour = int(stamp[11:13])
        out.append(round(max(0.0, peak * (1 - abs(hour - 13) / 7)), 1) if 6 <= hour <= 20 else 0.0)
    return out


def payload(
    *,
    temp=28.0,
    apparent=30.0,
    humidity=60.0,
    precip=0.0,
    precip_prob=10.0,
    weather_code=1,
    wind=10.0,
    gust=18.0,
    uv_peak=5.0,
    visibility=20000.0,
    now_hour=9,
    base_day=BASE_DAY,
) -> dict:
    """A complete forecast response with constant conditions, so a fact is easy to reason about."""
    times = _times(base_day, HOURS)
    constant = lambda value: [value] * HOURS
    return {
        "latitude": 23.25,
        "longitude": 77.4,
        "timezone": "Asia/Kolkata",
        "utc_offset_seconds": 19800,
        "current": {
            "time": f"{base_day}T{now_hour:02d}:00",
            "temperature_2m": temp,
            "apparent_temperature": apparent,
            "relative_humidity_2m": humidity,
            "precipitation": precip,
            "weather_code": weather_code,
            "cloud_cover": 40,
            "wind_speed_10m": wind,
            "wind_gusts_10m": gust,
            "uv_index": _uv_curve(uv_peak, times)[now_hour],
            "is_day": 1,
        },
        "hourly": {
            "time": times,
            "temperature_2m": constant(temp),
            "apparent_temperature": constant(apparent),
            "relative_humidity_2m": constant(humidity),
            "precipitation": constant(precip),
            "precipitation_probability": constant(precip_prob),
            "weather_code": constant(weather_code),
            "wind_speed_10m": constant(wind),
            "wind_gusts_10m": constant(gust),
            "uv_index": _uv_curve(uv_peak, times),
            "visibility": constant(visibility),
        },
        "daily": {
            "time": [base_day, "2026-07-16", "2026-07-17"],
            "precipitation_sum": [round(precip * 24, 1)] * 3,
            "precipitation_probability_max": [precip_prob] * 3,
            "wind_gusts_10m_max": [gust] * 3,
            "uv_index_max": [uv_peak] * 3,
            "weather_code": [weather_code] * 3,
        },
    }


SCENARIOS = {
    "pleasant": payload(temp=24.0, apparent=25.0, humidity=55.0, precip_prob=5.0, wind=8.0, uv_peak=4.0),
    "heavy_rain_system": payload(
        temp=25.0, apparent=27.0, humidity=92.0, precip=4.2, precip_prob=95.0,
        weather_code=65, wind=22.0, gust=38.0, uv_peak=2.0, visibility=3000.0,
    ),
    "strong_wind": payload(temp=26.0, apparent=26.0, wind=47.0, gust=63.0, precip_prob=15.0, uv_peak=5.0),
    "extreme_uv": payload(temp=33.0, apparent=35.0, uv_peak=11.0, wind=9.0, precip_prob=0.0, now_hour=13),
    "thunderstorm": payload(temp=27.0, apparent=29.0, weather_code=95, precip=1.5, precip_prob=80.0, gust=44.0),
    "hot_for_children": payload(temp=37.0, apparent=41.0, humidity=45.0, uv_peak=9.0, now_hour=15),
    "fog": payload(temp=14.0, apparent=13.0, visibility=400.0, weather_code=45, wind=5.0, precip_prob=0.0),
    "mild_uv": payload(temp=27.0, apparent=28.0, uv_peak=9.1, wind=12.0, precip_prob=20.0, now_hour=13),
}

PLACE = {
    "name": "Bhopal", "admin1": "Madhya Pradesh", "country": "India",
    "latitude": 23.25, "longitude": 77.4, "alternatives": [], "query": "Bhopal",
}
