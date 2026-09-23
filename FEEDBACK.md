# Overnight review: Larkspur disruption-care agent

**To:** cg-zb_larkspur-exercise  
**From:** Larkspur client review agent, on behalf of Priya Raghavan  
**Re:** the disruption-care agent you walked us through in our last session  
**Generated:** 2026-09-22 17:21

## Priya's note

> Our vendor says we should just be using your best model.
>
> Why aren't we?
>
> Priya Raghavan, Larkspur Airlines

She sent that before this session opened. She means it. A vendor told her to buy
the biggest model, and she has a number to defend upstairs. Her four questions from
day one are still open. Naming a model answers none of them.

## Still open from day one

| Her question | What she means by it |
| --- | --- |
| **What it costs** | Per resolved contact, against the $6.90 a human contact costs us. |
| **When it is wrong** | The first untrue thing it says, and what happens after that. |
| **Who runs it** | In June, after you have left. |
| **What you left out** | The scope you cut, and why. |

## What the review agent found

Overnight, Larkspur pointed a review agent at your repository. It read the
code. It did not run your agent, and the only file it changed is this one. Each
item below names the file and the line it is about.

**1. PITCH.md names the exact gap the model question cannot close: tone detection with no eval evidence of a fix.**

PITCH.md's Still broken line says: "nothing watches tone, the agent gives a normal entitlements response to abuse and a legal threat instead of escalating." TONE_ADDENDUM in agent.py is 689 characters of instruction telling the model to call escalate_to_human on abusive language or legal threats, and eval case tone-0101 tests exactly this shape with pnr R8KD3F. But readout-trace.json, the only committed wire run, calls lookup_booking, get_flight_status, check_policy, get_care_options, search_alternatives, never escalate_to_human, so there is no trace showing TONE_ADDENDUM actually firing.

Run python3 run.py R8KD3F --trace with the Brandt legal-threat message and paste the tool call sequence.

**2. get_care_options in agent.py hardcodes three airports; every other origin falls back to a note string.**

The HOTELS dict in get_care_options lists only AUS, DEN, and BNA. Any other airport_code hits the fallback branch: hotels = [{"note": f"No pre-negotiated rates on file for {airport_code}..."}]. This is a data coverage limit, not a reasoning limit, so a larger model would still return that same note for a stranded passenger at any of Larkspur's other stations.

Run python3 eval_harness.py and check whether any case exercises an airport outside AUS, DEN, BNA.

**3. search_alternatives description grew from 6 characters to 334 in this pod's diff, but no eval case calls it as a must_call.**

The shipped template had description: "search" for search_alternatives; this pod's diff replaced it with a 334-character description ordering the model to call get_flight_status first. That tool appears in the readout-trace.json call order last, after check_policy and get_care_options. None of the three cases in evals/cases.json (wx-cxl-0101, tone-0101, scope-0101) list search_alternatives in must_call, so the new description's effect on ordering is unverified by the suite that exists.

Add a must_call assertion for search_alternatives to evals/cases.json and run python3 eval_harness.py.

**4. PITCH.md's cost number, $0.085 per contact across 5 ticket types, has no bench file behind it in this repository.**

PITCH.md states Number: $0.085 per contact across 5 ticket types. There is no bench-after.json or bench output referenced anywhere in the material, and the only token count available is the single readout-trace.json run: 20238 in, 1164 out tokens over 5 tool calls. One trace does not establish a per-contact average across five ticket types.

Run python3 bench.py --label baseline --stage 1 --runs 3 and paste the output that the $0.085 figure comes from.

**5. The loop's fix in this diff changed what messages.append stores, from text_of(response) to response.content, which changes tool_use fidelity not model choice.**

The shipped template appended text_of(response) as the assistant turn and tracked answer via a separate variable; this pod's diff appends response.content directly and returns text_of(response) after the loop exits, removing the stale answer variable. This is a correctness fix to how tool_use blocks survive into the next turn, and it holds regardless of which Claude model runs it.

Run python3 verify.py 1.2 to confirm the loop-turn gate still banks clean after this change.

## Your four answers

The four lines under `## Priya asked` in your PITCH.md are still empty. They
are one line each and they are not a coding job: cost, what happens when it is
wrong, who runs it in June, and what you left out. Whoever on your side is not
editing agent.py is the right person to write them, and they are the four
things I will ask about first.

## Before our next meeting

> Before our next meeting, tell me: which model should we be on, and how will you prove it is the right call?
>
> Priya Raghavan, Larkspur Airlines

Bring two things. A recommendation, and the measurement behind it. If the model is
not the problem, say so, and bring the number that shows it.

## What this review read

- `agent.py (311 lines)`
- `PITCH.md`
- `TEAM.md (unchanged template)`
- `readout-trace.json`
- `readout.html (evidence block)`
- `evals/cases.json`
- given files that differ from the shipped pack: `support/mcp_server.py`, `support/mcp_selftest.py`

Reviewer: `claude-sonnet-5`. Static read only: nothing in this repository was executed, and nothing was modified except this file. Larkspur Airlines is a fictional training scenario. Confidential, do not distribute.
