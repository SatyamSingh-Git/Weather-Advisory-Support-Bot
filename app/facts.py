"""Turn a raw Open-Meteo payload into the flat fact table that SOP conditions are evaluated against.

Every numeric fact here traces to a value the API actually returned. Nothing in this module
consults the model, and the model never writes into the table this module produces.
"""

WINDOW_HOURS = {
    "morning": (6, 11),
    "afternoon": (12, 16),
    "evening": (17, 21),
    "night": (22, 23),
    "today": (6, 21),
    "tomorrow": (6, 21),
}

THUNDERSTORM_CODES = {95, 96, 99}


def _hour(ts: str) -> int:
    return int(ts[11:13])


def _date(ts: str) -> str:
    return ts[:10]


def _clean(values):
    return [v for v in values if v is not None]


def _max(values):
    vals = _clean(values)
    return round(max(vals), 1) if vals else None


def _min(values):
    vals = _clean(values)
    return round(min(vals), 1) if vals else None


def _mean(values):
    vals = _clean(values)
    return round(sum(vals) / len(vals), 1) if vals else None


def _sum(values):
    vals = _clean(values)
    return round(sum(vals), 1) if vals else None


def _window_indices(times, now_index, window):
    """Hourly indices covering the requested window.

    A window that has already passed today rolls forward to tomorrow, so "what about this
    evening" asked at 11pm answers about a window that still exists.
    """
    if window == "now":
        return [now_index], _date(times[now_index])

    low, high = WINDOW_HOURS[window]
    days = sorted({_date(t) for t in times})
    today = _date(times[now_index])
    target = days[days.index(today) + 1] if window == "tomorrow" else today

    def pick(day):
        return [i for i, t in enumerate(times) if _date(t) == day and low <= _hour(t) <= high]

    idx = [i for i in pick(target) if i >= now_index]
    if idx:
        return idx, target
    later = days[days.index(target) + 1:]
    if later:
        return pick(later[0]), later[0]
    return [now_index], today


def comfort_score(temp_c, precip_prob, wind_kmh, uv, humidity):
    """A deterministic 0-100 pleasantness score for questions with no natural threshold.

    Not a safety measure — it exists so that fuzzy SOPs ("is today nice for a picnic") still
    match on a checkable number rather than on the model's mood.
    """
    score = 100.0
    if temp_c is not None:
        score -= max(0.0, abs(temp_c - 24) - 5) * 3.5
    if precip_prob is not None:
        score -= precip_prob * 0.6
    if wind_kmh is not None:
        score -= max(0.0, wind_kmh - 25) * 1.5
    if uv is not None:
        score -= max(0.0, uv - 7) * 5
    if humidity is not None:
        score -= max(0.0, humidity - 80) * 0.5
    return round(max(0.0, min(100.0, score)), 1)


def build_facts(payload: dict, window: str) -> tuple[dict, dict]:
    """Return (facts, provenance). Provenance says, per fact, which API values produced it."""
    hourly = payload["hourly"]
    times = hourly["time"]
    current = payload["current"]
    now_stamp = current["time"][:13] + ":00"
    now_index = times.index(now_stamp) if now_stamp in times else 0

    idx, window_date = _window_indices(times, now_index, window)
    take = lambda field: [hourly[field][i] for i in idx] if field in hourly else []
    next24 = slice(now_index, now_index + 24)

    codes = [c for c in take("weather_code") if c is not None]
    daily = payload.get("daily", {})

    if window == "now":
        temp = round(current["temperature_2m"], 1)
        apparent = round(current["apparent_temperature"], 1)
        wind = round(current["wind_speed_10m"], 1)
        gust = round(current["wind_gusts_10m"], 1)
        uv = round(current["uv_index"], 1) if current.get("uv_index") is not None else _max(take("uv_index"))
        humidity = round(current["relative_humidity_2m"], 1)
        source = "open-meteo current observation"
    else:
        temp = _mean(take("temperature_2m"))
        apparent = _max(take("apparent_temperature"))
        wind = _max(take("wind_speed_10m"))
        gust = _max(take("wind_gusts_10m"))
        uv = _max(take("uv_index"))
        humidity = _mean(take("relative_humidity_2m"))
        source = f"open-meteo hourly forecast, {window_date} {times[idx[0]][11:16]}–{times[idx[-1]][11:16]}"

    facts = {
        "temp_c": temp,
        "apparent_temp_c": apparent,
        "wind_kmh": wind,
        "gust_kmh": gust,
        "uv_index": uv,
        "humidity_pct": humidity,
        "precip_prob_pct": _max(take("precipitation_probability")),
        "precip_mm": _sum(take("precipitation")),
        "visibility_m": _min(take("visibility")),
        "thunderstorm": bool(set(codes) & THUNDERSTORM_CODES),
        "rain_24h_mm": _sum(hourly["precipitation"][next24]),
        "gust_max_24h_kmh": _max(hourly["wind_gusts_10m"][next24]),
        "precip_prob_max_24h_pct": _max(hourly["precipitation_probability"][next24]),
        "daily_precip_sum_mm": round(daily["precipitation_sum"][0], 1) if daily.get("precipitation_sum") else None,
        "local_hour": _hour(current["time"]),
        "is_day": bool(current.get("is_day", 1)),
        "observed_at": current["time"],
        "window": window,
        "window_date": window_date,
    }
    facts["comfort_score"] = comfort_score(
        facts["temp_c"], facts["precip_prob_pct"], facts["wind_kmh"], facts["uv_index"], facts["humidity_pct"]
    )

    provenance = {k: source for k in ("temp_c", "apparent_temp_c", "wind_kmh", "gust_kmh", "uv_index", "humidity_pct")}
    provenance.update(
        {
            "precip_prob_pct": source,
            "precip_mm": source,
            "visibility_m": source,
            "thunderstorm": "open-meteo hourly weather_code",
            "rain_24h_mm": "open-meteo hourly precipitation, next 24h",
            "gust_max_24h_kmh": "open-meteo hourly wind_gusts_10m, next 24h",
            "precip_prob_max_24h_pct": "open-meteo hourly precipitation_probability, next 24h",
            "daily_precip_sum_mm": "open-meteo daily precipitation_sum, today",
            "comfort_score": "derived in app/facts.py from the values above",
        }
    )
    return facts, provenance


# Sent to the composer alongside each value. The model quoted a real number under the wrong label
# once (today's rainfall total described as "next 24 hours"), which the grounding check cannot
# catch, since it verifies where a number came from and not what it was called.
FACT_MEANINGS = {
    "temp_c": ("C", "air temperature"),
    "apparent_temp_c": ("C", "feels-like temperature"),
    "wind_kmh": ("km/h", "sustained wind speed"),
    "gust_kmh": ("km/h", "peak wind gust"),
    "uv_index": ("", "UV index"),
    "humidity_pct": ("%", "relative humidity"),
    "precip_prob_pct": ("%", "highest chance of rain within the window the user asked about"),
    "precip_mm": ("mm", "rainfall within the window the user asked about"),
    "visibility_m": ("m", "lowest visibility within the window the user asked about"),
    "rain_24h_mm": ("mm", "total rainfall forecast over the next 24 hours from now"),
    "gust_max_24h_kmh": ("km/h", "strongest gust forecast in the next 24 hours"),
    "precip_prob_max_24h_pct": ("%", "highest chance of rain in the next 24 hours"),
    "daily_precip_sum_mm": ("mm", "rainfall for the whole calendar day, midnight to midnight"),
    "comfort_score": ("/100", "derived pleasantness score, not a safety measure"),
}


def labelled(facts: dict) -> dict:
    """The fact table as the composer sees it: value, unit and what the number actually means."""
    table = {
        key: {"value": facts[key], "unit": unit, "means": means}
        for key, (unit, means) in FACT_MEANINGS.items()
        if facts.get(key) is not None
    }
    table["context"] = {
        key: facts[key]
        for key in ("location", "window", "window_date", "observed_at", "thunderstorm", "local_hour")
        if facts.get(key) is not None
    }
    return table
