# Writing a policy

This directory *is* the advice the bot gives. Nothing in `app/` decides what is safe; it only works
out which of these files applies and puts it into a sentence. If the advice is wrong, the fix is
here, not in the code.

You do not need to be able to read Python to edit these.

## A policy, end to end

```yaml
id: high_wind_two_wheeler              # unique, lowercase, never reused
title: Strong wind for cycling and two-wheelers
verdict: Not safe on two wheels        # the decision, shown large at the top of the reply
category: outdoor_exercise
severity: high                         # info | low | moderate | high | critical
priority: 20                           # breaks ties inside the same severity, higher wins
requires_facts: [wind_kmh]             # if we do not have this reading, do not match at all
when:
  - {fact: activity_category, op: includes_any, value: [outdoor_exercise, travel_commute]}
  - any_of:
      - {fact: wind_kmh, op: gt, value: 40}
      - {fact: gust_kmh, op: gte, value: 50}
guidance: >
  Treat this wind as a safety risk, not a comfort issue. Gusts at this strength can push a cyclist
  across a lane...
```

**`verdict` is the decision the user acts on.** Keep it short and imperative — "Postpone outdoor
plans", "Go, but allow extra time", "Clear to proceed". The model writes the explanation underneath
it but never the verdict itself, which is the point: the call is yours, not the model's.

**`guidance` is an instruction to whoever answers**, not text shown verbatim. Say what advice to
give and what to mention. Be specific — "advise postponing and warn about waterlogging on
underpasses" is checkable; "be careful" is not.

**`requires_facts` is the honesty valve.** List every reading the rule genuinely depends on. If the
weather service does not return one, the policy will not match rather than matching on a guess.

## The readings you can write rules against

| Fact | Unit | What it is |
| --- | --- | --- |
| `apparent_temp_c` | C | feels-like temperature |
| `comfort_score` | /100 | derived pleasantness score, not a safety measure |
| `daily_precip_sum_mm` | mm | rainfall for the whole calendar day, midnight to midnight |
| `gust_kmh` | km/h | peak wind gust |
| `gust_max_24h_kmh` | km/h | strongest gust forecast in the next 24 hours |
| `humidity_pct` | % | relative humidity |
| `precip_mm` | mm | rainfall within the window the user asked about |
| `precip_prob_max_24h_pct` | % | highest chance of rain in the next 24 hours |
| `precip_prob_pct` | % | highest chance of rain within the window the user asked about |
| `rain_24h_mm` | mm | total rainfall forecast over the next 24 hours from now |
| `temp_c` | C | air temperature |
| `thunderstorm` | — | whether a thunderstorm code appears in the window |
| `uv_index` | — | UV index |
| `visibility_m` | m | lowest visibility within the window the user asked about |
| `wind_kmh` | km/h | sustained wind speed |

And these describe the *question* rather than the weather:

| Fact | Type | What it is |
| --- | --- | --- |
| `activity_category` | list | What the user wants to do. One or more of: `outdoor_exercise`, `travel_commute`, `vulnerable_groups`, `leisure_social`, `outdoor_work` |
| `audience` | list | Who it is for. One or more of: `general`, `children`, `elderly`, `pets`, `outdoor_worker`, `expectant_or_unwell` |
| `is_outdoor_question` | true/false | Whether they are asking about doing something outdoors |
| `local_hour` | 0–23 | Hour of day at the location |
| `is_day` | true/false | Daylight at the location |

The time window a question is about is chosen automatically from what the user asked
(`now`, `morning`, `afternoon`, `evening`, `night`, `today`, `tomorrow`), and the readings above are computed for that window.
You do not write rules about the window; you write rules about the readings.

## Operators

| Operator | Meaning | Example |
| --- | --- | --- |
| `gte` `gt` `lte` `lt` | at least / more than / at most / less than | `{fact: uv_index, op: gte, value: 8}` |
| `eq` `ne` | equals / does not equal | `{fact: window, op: eq, value: evening}` |
| `between` | inclusive range | `{fact: comfort_score, op: between, value: [40, 71.9]}` |
| `in` | value is one of a list | `{fact: window, op: in, value: [morning, evening]}` |
| `includes_any` | a list fact overlaps yours | `{fact: audience, op: includes_any, value: [children]}` |
| `is_true` | a yes/no reading | `{fact: thunderstorm, op: is_true, value: true}` |

Conditions listed under `when` must **all** hold. Nest `any_of` or `all_of` inside it when you need
"either of these".

## Adding one

Drop a new `.yaml` file in this directory, or use the **Library** tab in the app. Either way it is
live on the next message — no restart, no deploy, no code change.

It is validated before it is accepted, and linted after. The lint catches the mistakes that would
otherwise make a rule quietly never fire:

- a fact name that does not exist (a typo in `uv_indx` can never be true)
- a reading the rule depends on but did not declare in `requires_facts`
- an operator given the wrong shape of value
- a missing verdict, or guidance too short to act on

## When two policies both apply

1. Anything marked `override: true` leads, regardless of severity. Use it only for situational risk
   that outranks individual thresholds — an active rain system makes a normal-looking wind reading
   dangerous.
2. Otherwise: highest `severity`, then highest `priority`, then whichever has more conditions.

The top one becomes the answer. The next two are shown as "also applies", so nothing is silently
dropped.

## What you cannot do here

Write a rule about a reading we do not collect. Air quality, pollen, lightning strike distance —
all of those need an engineer to add the fetch first. Everything else is this directory.
