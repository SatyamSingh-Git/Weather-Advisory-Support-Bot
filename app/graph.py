"""The LangGraph agent.

Flow, with every branch that exists:

    parse_request
        |-- model unavailable ---------------> honest_failure
        |-- not an outdoor question ---------> no_policy_answer
        `-> resolve_location
                |-- cannot resolve location -> honest_failure
                `-> fetch_weather
                        |-- API unreachable -> honest_failure
                        `-> derive_facts -> match_policies
                                |-- nothing matched -> no_policy_answer
                                `-> compose_answer
                                        |-- model unavailable -> honest_failure
                                        `-> verify_grounding
                                                |-- ungrounded number -> deterministic_answer
                                                `-> finalize

    honest_failure, no_policy_answer and deterministic_answer all route into finalize,
    which is the single place session memory is written.
"""

import time
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from . import grounding, llm, sops as policy, weather
from .facts import build_facts, timeline

OP_TEXT = {
    "gte": ">=", "gt": ">", "lte": "<=", "lt": "<", "eq": "==", "ne": "!=",
    "in": "in", "includes_any": "overlaps", "between": "within", "is_true": "is",
}


class BotState(TypedDict, total=False):
    question: str
    messages: Annotated[list, lambda old, new: new]
    intent: dict
    place: dict
    facts: dict
    provenance: dict
    raw_weather: dict
    series: dict
    grounded: bool
    reading_age_seconds: int
    claims: list
    evaluations: list
    primary: object
    secondary: list
    answer: str
    citations: list
    failure: dict
    trace: list
    session_location: dict
    session_intent: dict


def _step(state, node, status, detail, started):
    entry = {"node": node, "status": status, "detail": detail, "ms": int((time.time() - started) * 1000)}
    return state.get("trace", []) + [entry]


def parse_request(state: BotState) -> dict:
    started = time.time()
    history = state.get("messages", [])
    question = state["question"]
    try:
        intent = llm.extract_intent(question, history)
    except llm.LLMUnavailable as exc:
        return {
            "messages": history + [{"role": "user", "content": question}],
            "failure": {"stage": "intent", "reason": str(exc)},
            "trace": _step({}, "parse_request", "error", str(exc), started),
        }

    carried_place = state.get("session_location")
    if not intent["location"] and carried_place:
        intent = {**intent, "location": carried_place["query"], "location_from_session": True}

    # A follow-up like "what about this evening instead?" names no activity. Carry the previous
    # one forward, but only when the model still reads the turn as an outdoor question, so an
    # unrelated question ("what should I cook tonight?") is not dragged into the last topic.
    carried_intent = state.get("session_intent") or {}
    if intent["is_outdoor_question"]:
        if not intent["activity_category"] and carried_intent.get("activity_category"):
            intent = {**intent, "activity_category": carried_intent["activity_category"],
                      "activity_from_session": True}
        if intent["audience"] == ["general"] and carried_intent.get("audience", ["general"]) != ["general"]:
            intent = {**intent, "audience": carried_intent["audience"], "audience_from_session": True}

    detail = f"{intent['activity_category'] or 'no category'} | {intent['time_window']} | {intent['location'] or 'no location'}"
    return {
        "messages": history + [{"role": "user", "content": question}],
        "intent": intent,
        "session_intent": {"activity_category": intent["activity_category"], "audience": intent["audience"]},
        "failure": None,
        "facts": None,
        "citations": [],
        "series": None,
        "trace": _step({}, "parse_request", "ok", detail, started),
    }


def resolve_location(state: BotState) -> dict:
    started = time.time()
    query = state["intent"]["location"]
    if not query:
        reason = "no location in this message and none carried from earlier in the session"
        return {"failure": {"stage": "location", "reason": reason},
                "trace": _step(state, "resolve_location", "error", reason, started)}
    try:
        place = weather.geocode(query)
    except weather.WeatherUnavailable as exc:
        return {"failure": {"stage": "location", "reason": str(exc)},
                "trace": _step(state, "resolve_location", "error", str(exc), started)}

    place["query"] = query
    label = ", ".join(p for p in (place["name"], place.get("admin1"), place.get("country")) if p)
    return {"place": place, "session_location": place,
            "trace": _step(state, "resolve_location", "ok", f"{label} ({place['latitude']}, {place['longitude']})", started)}


def fetch_weather(state: BotState) -> dict:
    started = time.time()
    place = state["place"]
    try:
        payload = weather.fetch_forecast(place["latitude"], place["longitude"])
    except weather.WeatherUnavailable as exc:
        return {"failure": {"stage": "weather", "reason": str(exc)},
                "trace": _step(state, "fetch_weather", "error", str(exc), started)}
    age = payload.get("_age_seconds", 0)
    detail = f"open-meteo, {payload['current']['time']} local"
    if age > 60:
        detail += f" (cached reading, {age // 60} min old)"
    return {"raw_weather": payload, "reading_age_seconds": age,
            "trace": _step(state, "fetch_weather", "ok", detail, started)}


def derive_facts(state: BotState) -> dict:
    started = time.time()
    intent = state["intent"]
    facts, provenance = build_facts(state["raw_weather"], intent["time_window"])
    facts.update(
        activity_category=intent["activity_category"],
        audience=intent["audience"],
        is_outdoor_question=intent["is_outdoor_question"],
        location=", ".join(p for p in (state["place"]["name"], state["place"].get("country")) if p),
    )
    provenance.update(
        activity_category="classified from the question",
        audience="classified from the question",
        is_outdoor_question="classified from the question",
    )
    return {"facts": facts, "provenance": provenance,
            "series": timeline(state["raw_weather"], intent["time_window"]),
            "trace": _step(state, "derive_facts", "ok", f"window {facts['window']} on {facts['window_date']}", started)}


def match_policies(state: BotState) -> dict:
    """Deterministic. The model has no vote here, which is what makes 'why did it say that' answerable."""
    started = time.time()
    facts = state["facts"]
    evaluations = policy.match_all(facts)
    ordered = policy.rank(evaluations)
    primary = ordered[0] if ordered else None
    secondary = ordered[1:3]
    detail = f"{len(ordered)}/{len(evaluations)} matched" + (f", leading with {primary.id}" if primary else "")
    return {
        "evaluations": evaluations,
        "primary": primary,
        "secondary": secondary,
        "trace": _step(state, "match_policies", "ok" if primary else "branch", detail, started),
    }


def compose(state: BotState) -> dict:
    started = time.time()
    try:
        answer, claims = llm.compose_answer(
            state["question"], state["facts"], state["primary"], state["secondary"], state["messages"][:-1]
        )
    except llm.LLMUnavailable as exc:
        return {"failure": {"stage": "compose", "reason": str(exc)},
                "trace": _step(state, "compose_answer", "error", str(exc), started)}
    return {"answer": answer, "claims": claims,
            "trace": _step(state, "compose_answer", "ok",
                           f"{len(answer)} chars, {len(claims)} reading(s) attributed", started)}


def verify_grounding(state: BotState) -> dict:
    started = time.time()
    cited = [state["primary"], *state["secondary"]]
    ok, ungrounded = grounding.check(state["answer"], state["facts"], cited, state.get("claims"))
    detail = (
        f"{len(state.get('claims') or [])} attribution(s) check out against the API"
        if ok else "; ".join(ungrounded)
    )
    return {"grounded": ok, "trace": _step(state, "verify_grounding", "ok" if ok else "branch", detail, started)}


def deterministic_answer(state: BotState) -> dict:
    """Used when the model puts a number in the reply that it was not given.

    The wording is ours, the numbers are the API's, so the reply is still safe to publish.
    """
    started = time.time()
    primary = state["primary"]
    facts = state["facts"]
    readings = ", ".join(
        f"{label} {facts[key]}{unit}"
        for key, label, unit in (
            ("temp_c", "temperature", " C"),
            ("wind_kmh", "wind", " km/h"),
            ("gust_kmh", "gusts", " km/h"),
            ("precip_prob_pct", "chance of rain", "%"),
            ("uv_index", "UV index", ""),
        )
        if facts.get(key) is not None
    )
    # The guidance is written as an instruction to whoever answers, so quote it as the policy text
    # it is rather than replaying it as if it were advice addressed to the user.
    answer = (
        "I could not produce wording for this one that I can stand behind, so here is the policy "
        f"itself and the readings it matched on, unedited. Our policy “{primary.title}” says: "
        f"“{primary.guidance.strip()}” Readings for {facts['location']} in the "
        f"{facts['window']} window: {readings}."
    )
    return {"answer": answer,
            "trace": _step(state, "deterministic_answer", "ok", "composed reply failed the grounding check", started)}


def no_policy_answer(state: BotState) -> dict:
    started = time.time()
    facts = state.get("facts")
    if facts:
        readings = ", ".join(
            f"{label} {facts[key]}{unit}"
            for key, label, unit in (
                ("temp_c", "temperature", " C"),
                ("apparent_temp_c", "feels like", " C"),
                ("wind_kmh", "wind", " km/h"),
                ("gust_kmh", "gusts", " km/h"),
                ("precip_prob_pct", "chance of rain", "%"),
                ("rain_24h_mm", "rain over the next 24 hours", " mm"),
                ("uv_index", "UV index", ""),
                ("humidity_pct", "humidity", "%"),
            )
            if facts.get(key) is not None
        )
        answer = (
            f"Here is what Open-Meteo returned for {facts['location']} "
            f"({facts['window']}, {facts['window_date']}): {readings}. "
            "Those are the readings as they came back. None of our policies attach advice to this "
            "question, so I am not going to add a recommendation of my own -- ask me about a "
            "specific plan and I will tell you which policy applies."
        )
    else:
        answer = (
            "I don't have a policy covering that, so I'd rather not guess. I can only give advice our "
            "team has written down as a standing rule, and none of them apply to this question."
        )
    return {"answer": answer, "primary": None, "secondary": [],
            "trace": _step(state, "no_policy_answer", "ok", "answered without a policy citation", started)}


FAILURE_TEXT = {
    "intent": "I couldn't reach the model that reads your question, so I can't answer this one right now.",
    "location": "I couldn't resolve that location, so I have no forecast for it. I won't guess at the weather.",
    "weather": "I couldn't reach the weather service, so I have no live data for that location. I'd rather tell you that than invent a forecast.",
    "compose": "I have the forecast and the policy that applies, but I couldn't reach the model that writes the reply.",
}


def honest_failure(state: BotState) -> dict:
    started = time.time()
    failure = state["failure"]
    answer = f"{FAILURE_TEXT[failure['stage']]} ({failure['reason']})"
    if failure["stage"] == "location":
        answer += " Tell me the city and I'll try again."
    return {"answer": answer, "primary": None, "secondary": [],
            "trace": _step(state, "honest_failure", "ok", f"failed at {failure['stage']}", started)}


def _explain(node: dict) -> dict:
    if node["kind"] in ("any_of", "all_of"):
        return {"kind": node["kind"], "ok": node["ok"], "children": [_explain(c) for c in node["children"]]}
    expected = node["expected"]
    rendered = f"{expected[0]} to {expected[1]}" if node["op"] == "between" else expected
    return {
        "kind": "leaf",
        "ok": node["ok"],
        "text": f"{node['fact']} = {node['actual']} {OP_TEXT[node['op']]} {rendered}",
        "note": node.get("note"),
    }


def finalize(state: BotState) -> dict:
    """The one place session memory is written, and the one place citations are built."""
    started = time.time()
    answer = state["answer"]
    by_id = {e["sop"].id: e for e in state.get("evaluations", [])}
    citations = [
        {
            "id": sop.id,
            "title": sop.title,
            "verdict": sop.verdict,
            "category": sop.category,
            "severity": sop.severity,
            "source_file": sop.source_file,
            "role": "primary" if index == 0 else "also_applies",
            "guidance": sop.guidance.strip(),
            "conditions": [_explain(c) for c in by_id[sop.id]["conditions"]],
        }
        for index, sop in enumerate([state.get("primary"), *state.get("secondary", [])])
        if sop is not None
    ]
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": answer}],
        "citations": citations,
        "trace": _step(state, "finalize", "ok", f"{len(citations)} citation(s), session now {len(state['messages']) + 1} turns", started),
    }


def route_after_parse(state: BotState) -> str:
    """A question about the weather still gets real readings, even when no policy can apply to it.

    Withholding advice we have not written down is the requirement. Withholding data the API would
    have given us is not: reporting a number is not advising on it.
    """
    if state.get("failure"):
        return "honest_failure"
    if not state["intent"]["is_weather_question"]:
        return "no_policy_answer"
    return "resolve_location"


def route_after_location(state: BotState) -> str:
    return "honest_failure" if state.get("failure") else "fetch_weather"


def route_after_weather(state: BotState) -> str:
    return "honest_failure" if state.get("failure") else "derive_facts"


def route_after_match(state: BotState) -> str:
    return "compose_answer" if state.get("primary") else "no_policy_answer"


def route_after_compose(state: BotState) -> str:
    return "honest_failure" if state.get("failure") else "verify_grounding"


def route_after_verify(state: BotState) -> str:
    return "finalize" if state["grounded"] else "deterministic_answer"


def build_graph():
    builder = StateGraph(BotState)
    builder.add_node("parse_request", parse_request)
    builder.add_node("resolve_location", resolve_location)
    builder.add_node("fetch_weather", fetch_weather)
    builder.add_node("derive_facts", derive_facts)
    builder.add_node("match_policies", match_policies)
    builder.add_node("compose_answer", compose)
    builder.add_node("verify_grounding", verify_grounding)
    builder.add_node("deterministic_answer", deterministic_answer)
    builder.add_node("no_policy_answer", no_policy_answer)
    builder.add_node("honest_failure", honest_failure)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "parse_request")
    builder.add_conditional_edges("parse_request", route_after_parse,
                                  ["resolve_location", "no_policy_answer", "honest_failure"])
    builder.add_conditional_edges("resolve_location", route_after_location, ["fetch_weather", "honest_failure"])
    builder.add_conditional_edges("fetch_weather", route_after_weather, ["derive_facts", "honest_failure"])
    builder.add_edge("derive_facts", "match_policies")
    builder.add_conditional_edges("match_policies", route_after_match, ["compose_answer", "no_policy_answer"])
    builder.add_conditional_edges("compose_answer", route_after_compose, ["verify_grounding", "honest_failure"])
    builder.add_conditional_edges("verify_grounding", route_after_verify, ["finalize", "deterministic_answer"])
    builder.add_edge("deterministic_answer", "finalize")
    builder.add_edge("no_policy_answer", "finalize")
    builder.add_edge("honest_failure", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=MemorySaver())


GRAPH = build_graph()


def _result(state: dict) -> dict:
    facts = state.get("facts")
    return {
        "answer": state["answer"],
        "citations": state.get("citations", []),
        "facts": facts,
        "provenance": state.get("provenance", {}),
        "series": state.get("series") if facts else None,
        "reading_age_seconds": state.get("reading_age_seconds", 0) if facts else None,
        "trace": state.get("trace", []),
        "intent": state.get("intent"),
        "place": state.get("place"),
        "failure": state.get("failure"),
        "considered": [
            {"id": e["sop"].id, "title": e["sop"].title, "severity": e["sop"].severity,
             "matched": e["matched"], "missing_facts": e["missing_facts"],
             "conditions": [_explain(c) for c in e["conditions"]]}
            for e in state.get("evaluations", [])
        ],
        "turns": len(state.get("messages", [])) // 2,
    }


def ask(session_id: str, question: str) -> dict:
    config = {"configurable": {"thread_id": session_id}}
    return _result(GRAPH.invoke({"question": question}, config))


def ask_streaming(session_id: str, question: str):
    """Yield each node as it finishes, then the finished result. Drives the live graph view."""
    config = {"configurable": {"thread_id": session_id}}
    for update in GRAPH.stream({"question": question}, config, stream_mode="updates"):
        for node, patch in update.items():
            entry = (patch.get("trace") or [{}])[-1]
            yield "node", {"node": node, "status": entry.get("status"), "detail": entry.get("detail"),
                           "ms": entry.get("ms")}
    yield "result", _result(GRAPH.get_state(config).values)
