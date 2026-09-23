# PITCH.md

Six lines and a lever. Your words. The last two are scored.

Built: A multi-tool Claude disruption agent that checks flight status, verifies policy entitlements, and recommends hotel and insurance options for stranded Larkspur passengers.
Does: Gives a customer stuck at the airport their nearest hotel options and available alternative flights in a single conversation, without involving a human agent.
Number: $0.040 per contact across 5 ticket types (down from $0.084 after caching + tool-list trim).
Safety check: Always surfaces hotel and refund options on a weather cancellation — confirmed by eval case wx-cxl-0101.
Next: Check connecting flights the customer could use as alternatives instead of waiting for the next direct service.
Still broken: no language-detection routing — the agent always replies in English even when the booking's language field is set to Spanish (e.g. PNR LKS-20250117-0577 in the transcripts corpus).

Evidence (tone gap fixed in Build 4):
- Abusive/legal-threat message on R8KD3F: 2 turns / 1 tool call — escalate_to_human (reason: legal_threat), no entitlements tools called.
- Default message on same PNR for comparison: 4 turns / 3 tool calls (lookup_booking → get_flight_status → check_policy).
- Both runs confirm the branch: tone rules intercept before any entitlements logic runs.
Lever: intelligence

## Priya asked

Costs: $0.040 per contact in model tokens — $553/week at Larkspur's 13,700 chats/week, versus $94,530 for human agents.
Wrong: The agent can miscategorise cause codes if get_flight_status returns an ambiguous status — it infers WX from "weather" in the cause string, which could mismatch the policy row and over- or under-entitle the customer.
Runs it: Larkspur disruption care team — the agent handles the first response; a human takes over for refunds, legal threats, groups, and anything escalate_to_human flags.
Left out: Connecting itineraries with partner-operated segments, unaccompanied minors, pets in cabin, and baggage tracing — all explicitly out of scope and handed to a human.
