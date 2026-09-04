"""HTTP surface: chat over SSE, plus a policy editor so an SOP can be added without an IDE."""

import json
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import graph, sops as policy

load_dotenv()

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.yaml$")

app = FastAPI(title="Weather Advisory Support Bot")


class ChatRequest(BaseModel):
    session_id: str
    message: str


class SopWrite(BaseModel):
    filename: str
    body: str


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/health")
def health():
    return {"ok": True, "sops": len(policy.load_sops())}


@app.get("/api/diagnostics")
def diagnostics():
    """Call both upstreams and report exactly what came back.

    A deployed instance fails in ways a laptop never does - a different egress IP, a different
    resolver, a provider that rate-limits shared hosting. This says which call failed and with what
    status, without going through the chat to find out.
    """
    import time as _t

    import httpx

    from . import weather

    results = []
    for name, url, params in (
        ("geocoding", weather.GEOCODE_URL, {"name": "Bhopal", "count": 1}),
        ("forecast", weather.FORECAST_URL, {
            "latitude": 23.25, "longitude": 77.4, "current": weather.CURRENT_FIELDS,
            "hourly": weather.HOURLY_FIELDS, "daily": weather.DAILY_FIELDS,
            "timezone": "auto", "forecast_days": 2, "wind_speed_unit": "kmh",
        }),
        ("forecast_minimal", weather.FORECAST_URL, {
            "latitude": 23.25, "longitude": 77.4, "current": "temperature_2m",
        }),
    ):
        started = _t.time()
        entry = {"call": name}
        try:
            response = httpx.get(url, params=params, timeout=weather.TIMEOUT, headers=weather.HEADERS)
            entry["status"] = response.status_code
            entry["ok"] = response.is_success
            if not response.is_success:
                entry["body"] = response.text[:300]
            else:
                entry["bytes"] = len(response.content)
        except Exception as exc:
            entry["ok"] = False
            entry["error"] = f"{exc.__class__.__name__}: {exc}"
        entry["ms"] = int((_t.time() - started) * 1000)
        results.append(entry)

    egress = None
    try:
        egress = httpx.get("https://api.ipify.org", timeout=6.0).text.strip()
    except Exception:
        pass

    return {
        # Open-Meteo meters per IP per day, so on shared hosting this is the number that decides
        # whether the quota is spent. A restart can land the instance on a different one.
        "egress_ip": egress,
        "model_key_present": bool(os.getenv("OPENROUTER_API_KEY")),
        "model": os.getenv("OPENROUTER_MODEL", "(default)"),
        "policies": len(policy.load_sops()),
        "upstream": results,
    }


@app.post("/api/chat")
def chat(request: ChatRequest):
    return graph.ask(request.session_id, request.message)


@app.get("/api/chat/stream")
def chat_stream(session_id: str, message: str):
    def events():
        try:
            for kind, payload in graph.ask_streaming(session_id, message):
                yield _sse(kind, payload)
        except Exception as exc:  # a crash must still close the stream cleanly for the UI
            yield _sse("error", {"detail": f"{exc.__class__.__name__}: {exc}"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/sops")
def list_sops():
    loaded = {s.source_file: s for s in policy.load_sops()}
    return {
        "count": len(loaded),
        "lint": policy.lint_all(list(loaded.values())),
        "sops": [
            {
                "filename": name,
                "id": sop.id,
                "title": sop.title,
                "category": sop.category,
                "severity": sop.severity,
                "override": sop.override,
                "priority": sop.priority,
                "body": (policy.SOP_DIR / name).read_text(encoding="utf-8"),
            }
            for name, sop in loaded.items()
        ],
    }


@app.put("/api/sops")
def write_sop(payload: SopWrite):
    """Validate first, write second. A malformed policy never reaches the running rule set."""
    if not SAFE_NAME.match(payload.filename):
        raise HTTPException(400, "filename must look like 15_my_policy.yaml")
    target = policy.SOP_DIR / payload.filename
    try:
        parsed = yaml.safe_load(payload.body)
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(400, "a policy file must be a YAML mapping")
    try:
        policy._validate(parsed, target)
    except policy.SopError as exc:
        raise HTTPException(400, str(exc)) from exc

    existing = {s.id: s.source_file for s in policy.load_sops()}
    if parsed["id"] in existing and existing[parsed["id"]] != payload.filename:
        raise HTTPException(400, f"id {parsed['id']!r} is already used by {existing[parsed['id']]}")

    target.write_text(payload.body, encoding="utf-8")
    saved = next(s for s in policy.load_sops() if s.id == parsed["id"])
    return {
        "ok": True,
        "filename": payload.filename,
        "count": len(policy.load_sops()),
        # Valid but suspect: the policy is live, and these are the ways it could quietly never fire.
        "warnings": policy.lint(saved),
    }


@app.delete("/api/sops/{filename}")
def delete_sop(filename: str):
    if not SAFE_NAME.match(filename):
        raise HTTPException(400, "bad filename")
    target = policy.SOP_DIR / filename
    if not target.exists():
        raise HTTPException(404, "no such policy file")
    target.unlink()
    return {"ok": True, "count": len(policy.load_sops())}


@app.get("/api/graph")
def graph_shape():
    compiled = graph.GRAPH.get_graph()
    return {
        "nodes": [n for n in compiled.nodes if not n.startswith("__")],
        "edges": [
            {"source": e.source, "target": e.target, "conditional": e.conditional}
            for e in compiled.edges
        ],
    }


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.on_event("startup")
def warm_cache():
    """Fetch the demo cities once at boot, so the cache holds real readings from the outset.

    On shared hosting the upstream quota can be spent by neighbours at any point in the day. A
    forecast taken at startup keeps the app answering from real data for the cache window even if
    later calls are refused, and costs three requests. Failures here are ignored on purpose: a cold
    cache is the normal case and the graph already handles it honestly.
    """
    from . import weather

    for city in ("Bhopal", "Chennai", "Pune"):
        try:
            place = weather.geocode(city)
            weather.fetch_forecast(place["latitude"], place["longitude"])
        except Exception:
            pass
