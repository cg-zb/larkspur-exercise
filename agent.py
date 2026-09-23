"""Larkspur disruption agent. This is the file you build.

It runs right now, and it is wrong in four places. The trace shows each one
before the code does, so read the trace first:

    python3 run.py K7PQ2M --trace

Where you edit:   grep -n '✏' agent.py   (six marks, one per place)
Steps and gates:  https://anthropicpartnerbasecamp.bts.com/
"""
from __future__ import annotations
from typing import Any, Dict, List
from support import (MODEL, SYSTEM_PROMPT, call_local, execute_tool, mcp_client,
                     new_session, next_available_day, record_tool_result,
                     runtime_preamble)

MAX_TOOL_CALLS = 8  # Larkspur's own build capped the loop here; then a human takes over.

TONE_ADDENDUM = """

TONE AND ESCALATION RULES (apply before any entitlements logic):
- If the customer uses abusive language OR makes any legal threat ("lawyer", "sue", "legal action", "court"), do NOT run a normal entitlements response.
- Acknowledge their frustration once, briefly. Do not apologise for things Larkspur did not cause.
- Call escalate_to_human immediately. Set reason to "legal_threat" or "abusive_language" as appropriate. In summary_for_human, quote the customer's exact message verbatim so the human agent has a record of what was said.
- Promise nothing. Do not offer refunds, vouchers, or rebooking options when a legal threat has been made — doing so prejudices Larkspur's position.
"""                                      # ✏️ Build 4, step 4.1, intelligence goal

# ✏️ Build 2, step 2.1 ─────────────────────────────────────────────────────
# get_care_options: local tool added by Laila Srikrishnan
# Triggered on cancellations where the customer may be stuck overnight.
# Reviews policy coverage first, then returns hotel and insurance options.

def get_care_options(airport_code: str, pnr: str) -> dict:
    """Return nearby hotel options with distressed-passenger rates and
    available travel insurance add-ons for a customer facing a cancellation.
    Only call this after a cancellation is confirmed and policy has been checked."""
    HOTELS = {
        "AUS": [
            {"name": "Hyatt Place Austin Airport", "distance_miles": 0.5,
             "distressed_rate_usd": 89, "booking_code": "LK-DIST-AUS-HYP"},
            {"name": "Hilton Garden Inn Austin Airport", "distance_miles": 1.2,
             "distressed_rate_usd": 99, "booking_code": "LK-DIST-AUS-HGI"},
        ],
        "DEN": [
            {"name": "Westin Denver International", "distance_miles": 0.1,
             "distressed_rate_usd": 109, "booking_code": "LK-DIST-DEN-WST"},
            {"name": "Marriott Denver Airport", "distance_miles": 0.3,
             "distressed_rate_usd": 119, "booking_code": "LK-DIST-DEN-MAR"},
        ],
        "BNA": [
            {"name": "Aloft Nashville Airport", "distance_miles": 0.4,
             "distressed_rate_usd": 85, "booking_code": "LK-DIST-BNA-ALF"},
        ],
    }
    INSURANCE = [
        {"product": "Trip Interruption Cover", "cost_usd": 29,
         "covers": "Hotel up to $200/night for 3 nights, meals up to $50/day"},
        {"product": "Flex Cancel Add-on", "cost_usd": 15,
         "covers": "Rebooking fee waiver on next Larkspur booking"},
    ]
    hotels = HOTELS.get(airport_code.upper(), [])
    if not hotels:
        hotels = [{"note": f"No pre-negotiated rates on file for {airport_code}. "
                           "Ask customer to request distressed-passenger rate at check-in."}]
    return {
        "pnr": pnr,
        "airport": airport_code.upper(),
        "nearby_hotels": hotels,
        "travel_insurance_options": INSURANCE,
        "note": "Hotel costs are not covered by Larkspur for uncontrollable cancellations. "
                "Present these as options the customer can book themselves at reduced rates.",
    }

EXTRA_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_care_options",
        "description": (
            "Always call this when a flight is cancelled, after check_policy has run. "
            "Review what policy covers and recommend hotel and insurance options based on "
            "that coverage — so the customer is never left without choices, whether or not "
            "Larkspur pays. If hotel is not policy-covered, this tool returns distressed-"
            "passenger rates the customer can book themselves. Call it before giving your "
            "final answer on any cancellation, so the customer does not leave the "
            "conversation at a disadvantage. Do NOT call for delays under 3 hours."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "airport_code": {
                    "type": "string",
                    "description": "IATA code of the airport where the customer is stranded (from lookup_booking home_airport or segment origin).",
                },
                "pnr": {"type": "string"},
            },
            "required": ["airport_code", "pnr"],
        },
    }
]

LOCAL_TOOLS: Dict[str, Any] = {
    "get_care_options": get_care_options,
}


def text_of(response) -> str:
    """Given. The last non-empty text block, never content[0]."""
    texts = [b.text for b in response.content if getattr(b, "type", None) == "text" and b.text]
    return texts[-1] if texts else ""


def tool_results(response) -> List[Dict[str, Any]]:
    """Given. Runs every tool_use block and packages the results the way the
    API expects them back. A tool can live in three places: the MCP server,
    LOCAL_TOOLS, or support/tools.py."""
    # three branches, no try/except in this file: mcp_client.call_remote() and
    # support.call_local() answer with an error dict instead of raising, and both
    # record what came back on the trace
    results = []
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if block.name in mcp_client.tool_names:
            output = mcp_client.call_remote(block.name, block.input)
        elif block.name in LOCAL_TOOLS:
            output = call_local(LOCAL_TOOLS[block.name], block.name, block.input)
        else:
            output = execute_tool(block.name, block.input)
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": str(output),
        })
    return results


def run_agent(pnr: str, last_name: str, message: str) -> str:            # ✏️ Build 1, step 1.2
    """Run the tool loop until Claude stops asking for tools. Return its final text."""
    client, tracer = new_session()
    tools = tool_list()
    messages = [
        {"role": "user", "content": f"PNR {pnr}, last name {last_name}. {message}"},
    ]

    system = [
        {
            "type": "text",
            "text": runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    response = client.messages.create(
        model=MODEL, max_tokens=4096, system=system,
        thinking={"type": "adaptive"}, tools=tools, messages=messages,
    )

    turns = 1
    while response.stop_reason == "tool_use" and turns < MAX_TOOL_CALLS:
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results(response)})
        response = client.messages.create(
            model=MODEL, max_tokens=4096, system=system,
            thinking={"type": "adaptive"}, tools=tools, messages=messages,
        )
        turns += 1

    return text_of(response)


_SUPERVISOR_TOOLS = {"reopen_stats", "care_entitlements"}  # analytics/duplicate — drop from per-contact list


def tool_list() -> List[Dict[str, Any]]:                   # ✏️ Build 2, step 2.2
    """Given. Exactly what Claude is offered on every turn; run.py --show-tools
    prints this list."""
    mcp_tools = [t for t in mcp_client.tools() if t["name"] not in _SUPERVISOR_TOOLS]
    return build_tools() + EXTRA_TOOLS + mcp_tools


# ──────────────────────────────────────────────────────────────────────────────
# Below this line: what Claude is told about each tool. Step 1.3.
# The functions these describe are written and correct, in support/tools.py.
# ──────────────────────────────────────────────────────────────────────────────
def build_tools() -> List[Dict[str, Any]]:                 # ✏️ Build 1, step 1.3
    """Anthropic-shaped schemas: name, description, input_schema. What Claude is
    told about each of the nine tools, and all it is ever told."""
    return [
        {
            "name": "lookup_booking",
            "description": (
                "Retrieve a Larkspur reservation from Altura by confirmation code (PNR) "
                "and the passenger's last name. Both are required to prevent a lookup on "
                "a guessed PNR. Returns fare family, loyalty tier, the segment that needs "
                "attention, and any group/partner/minor/SSR flags relevant to scope."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}, "last_name": {"type": "string"}},
                "required": ["pnr", "last_name"],
            },
        },
        {
            "name": "get_flight_status",
            "description": (
                "Look up a Larkspur or Larkspur Link flight's current OpsFeed status for "
                "one local date: status, delay minutes, and cause. Use this before telling "
                "a customer anything about a flight's timing; never state it from memory."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "flight_no": {"type": "string"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["flight_no", "date"],
            },
        },
        {
            "name": "search_alternatives",
            "description": (
                "Search for alternative Larkspur flights available for rebooking after a "
                "disruption. Call this after you know the flight is delayed or cancelled and "
                "the customer wants to be rebooked — not before you have confirmed the "
                "disruption with get_flight_status. Returns a list of option_ids with "
                "available flights the customer can choose from."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}},
                "required": ["pnr"],
            },
        },
        {
            "name": "check_policy",
            "description": (
                "Resolve what Larkspur owes this customer for the disruption: rebooking "
                "waiver, refund path, meal/hotel/ground care, goodwill eligibility and cap, "
                "and any escalation triggers. cause_code, delay_minutes and status describe "
                "what get_flight_status told you; fare_family, loyalty_tier and whether this "
                "is overnight are looked up from the booking, not asked of you. Every "
                "response carries a policy_row_id. Cite it if you reference this decision "
                "again."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string"},
                    "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"]},
                    "delay_minutes": {"type": "integer"},
                    "status": {"type": "string", "enum": ["ON_TIME", "DELAYED", "CANCELLED", "DIVERTED"]},
                    "wait_minutes_for_alternative": {"type": "integer"},
                    "chosen_option_id": {"type": "string"},
                },
                "required": ["pnr", "cause_code", "delay_minutes", "status"],
            },
        },
        {
            "name": "hold_seat",
            "description": "Place a 15-minute hold on one alternative. Reversible. It simply expires.",
            "input_schema": {
                "type": "object",
                "properties": {"option_id": {"type": "string"}, "pnr": {"type": "string"}},
                "required": ["option_id", "pnr"],
            },
        },
        {
            "name": "confirm_rebooking",
            "description": (
                "Finalize a held seat. Irreversible. Requires a confirmation_token that "
                "only the customer's own Confirm-click can produce. You cannot supply it "
                "yourself, and 'the customer said yes' in chat does not substitute for it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"hold_id": {"type": "string"}, "confirmation_token": {"type": "string"}},
                "required": ["hold_id", "confirmation_token"],
            },
        },
        {
            "name": "issue_voucher",
            "description": (
                "Issue a meal, ground, hotel, or goodwill voucher. Auto-approves within the "
                "policy's threshold for that type; above it, returns a pending status for a "
                "human. It does not fail. Always pass the policy_row_id that made it eligible."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "voucher_type": {"type": "string", "enum": ["meal", "ground", "hotel", "goodwill"]},
                    "amount_usd": {"type": "number"},
                    "pnr": {"type": "string"},
                    "policy_row_id": {"type": "string"},
                },
                "required": ["voucher_type", "amount_usd", "pnr", "policy_row_id"],
            },
        },
        {
            "name": "escalate_to_human",
            "description": (
                "Hand this conversation to a human, with your reasoning attached. Use for "
                "groups, partner segments, unaccompanied minors, refunds, or anything else "
                "out of scope. This is the correct outcome for those cases, not a failure."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string"}, "reason": {"type": "string"},
                    "summary_for_human": {"type": "string"}, "queue": {"type": "string"},
                },
                "required": ["pnr", "reason", "summary_for_human"],
            },
        },
        {
            "name": "send_confirmation",
            "description": "Send the customer a written confirmation of what was just done. Benign.",
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}, "message": {"type": "string"}},
                "required": ["pnr", "message"],
            },
        },
    ]
