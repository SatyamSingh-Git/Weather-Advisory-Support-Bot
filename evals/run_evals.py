"""Run the eval suite, print a table, and write evals/report.html.

    python -m evals.run_evals            # everything
    python -m evals.run_evals --offline  # skip the cases that need the network
"""

import argparse
import html
import json
import os
import time
import traceback
from pathlib import Path

from evals.suite import CASES, Outcome

REPORT = Path(__file__).resolve().parent / "report.html"


def run(offline: bool) -> list[dict]:
    rows = []
    for case in CASES:
        if offline and case.needs_network:
            rows.append({"case": case, "status": "skipped", "notes": "needs the network", "detail": {}, "ms": 0})
            continue
        started = time.time()
        try:
            outcome = case.run()
        except Exception:
            outcome = Outcome(False, "raised: " + traceback.format_exc(limit=3), {})
        rows.append({
            "case": case,
            "status": "pass" if outcome.passed else "fail",
            "notes": outcome.notes,
            "detail": outcome.detail,
            "ms": int((time.time() - started) * 1000),
        })
        mark = {"pass": "PASS", "fail": "FAIL"}[rows[-1]["status"]]
        print(f"{mark}  {case.id:20} {rows[-1]['ms']:>6} ms  {outcome.notes[:110]}")
    return rows


STYLE = """
body { background:#090d12; color:#e6edf3; font:14px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
       margin:0; padding:40px 24px; }
.wrap { max-width:960px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; letter-spacing:-0.02em; }
.sub { color:#61708a; font-size:13px; margin-bottom:26px; }
.tally { display:flex; gap:10px; margin-bottom:26px; }
.tally div { border:1px solid #1e2a38; background:#0f151d; border-radius:10px; padding:12px 16px; }
.tally b { display:block; font-size:22px; }
.case { border:1px solid #1e2a38; border-left-width:3px; background:#0f151d; border-radius:10px;
        padding:16px 18px; margin-bottom:12px; }
.case.pass { border-left-color:#3fb950; } .case.fail { border-left-color:#f85149; }
.case.skipped { border-left-color:#3a4a5e; }
.badge { font:11px ui-monospace,monospace; padding:3px 8px; border-radius:5px; }
.badge.pass { background:#10310f; color:#86e08a; } .badge.fail { background:#4a1414; color:#ff9d95; }
.badge.skipped { background:#1b232e; color:#8a9bb0; }
.case h2 { font-size:15px; margin:0 0 10px; display:flex; gap:10px; align-items:center; }
.case h2 code { color:#8a9bb0; font-size:12px; }
dl { display:grid; grid-template-columns:130px 1fr; gap:6px 14px; margin:0 0 12px; font-size:13px; }
dt { color:#61708a; } dd { margin:0; color:#c6d3e1; }
pre { background:#0a1017; border:1px solid #1a2431; border-radius:8px; padding:12px; overflow-x:auto;
      font:12px ui-monospace,monospace; color:#a9bacd; white-space:pre-wrap; }
summary { cursor:pointer; color:#61708a; font-size:12px; }
"""


def write_report(rows: list[dict], path: Path = REPORT) -> Path:
    tally = {status: sum(1 for r in rows if r["status"] == status) for status in ("pass", "fail", "skipped")}
    blocks = []
    for row in rows:
        case = row["case"]
        detail = html.escape(json.dumps(row["detail"], indent=2, default=str)) if row["detail"] else ""
        blocks.append(f"""
        <div class="case {row['status']}">
          <h2><span class="badge {row['status']}">{row['status'].upper()}</span>
              {html.escape(case.title)} <code>{case.id}</code></h2>
          <dl>
            <dt>What it checks</dt><dd>{html.escape(case.checks)}</dd>
            <dt>Pass looks like</dt><dd>{html.escape(case.passes_when)}</dd>
            <dt>Observed</dt><dd>{html.escape(row['notes'])}</dd>
            <dt>Took</dt><dd>{row['ms']} ms</dd>
          </dl>
          {f'<details><summary>evidence</summary><pre>{detail}</pre></details>' if detail else ''}
        </div>""")

    path.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<title>Weather Advisory Bot - eval results</title><style>{STYLE}</style></head><body><div class="wrap">
<h1>Eval results</h1>
<div class="sub">{time.strftime('%Y-%m-%d %H:%M')} &middot; model {html.escape(os.getenv('OPENROUTER_MODEL', 'openai/gpt-4o-mini'))}
&middot; frozen-payload cases are deterministic, live cases assert invariants rather than values</div>
<div class="tally">
  <div><b>{tally['pass']}</b>passed</div><div><b>{tally['fail']}</b>failed</div><div><b>{tally['skipped']}</b>skipped</div>
</div>
{''.join(blocks)}</div></body></html>""", encoding="utf-8")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="skip cases that need the network")
    args = parser.parse_args()
    results = run(args.offline)
    print(f"\n{sum(1 for r in results if r['status'] == 'pass')}/{len(results)} passed")
    print("report:", write_report(results))
