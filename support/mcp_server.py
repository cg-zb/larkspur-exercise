#!/usr/bin/env python3
"""An MCP server for two Larkspur lookups, written in the standard library only.

WHAT THIS IS, IN PLAIN WORDS

An MCP server is a small program that answers questions over its own stdin and
stdout. It does not talk to Claude. It does not know what a model is. Something
else, called the host, launches it as a subprocess and does the talking. Claude
Code is a host. `support/mcp_client.py` is a host. Both drive this same file.

The wire is one JSON object per line, in both directions. That is the whole
transport. Requests go in on stdin, responses come out on stdout, and anything
this file wants to say to a human goes to stderr, because a stray print on
stdout is a protocol error, not a log line.

THE MESSAGES YOU WILL SEE GO PAST

Run the client with tracing on and you will watch these:

    server/discover     who are you, what versions do you speak, what can you do
    tools/list          give me every tool you have, with its JSON schema
    tools/call          run this tool with these arguments, here is the answer
    ping                still there?

Older hosts open with `initialize` and then a `notifications/initialized`
notification instead of `server/discover`. This file answers both, because the
protocol changed shape in the 2026-07-28 revision and real hosts are still
mixed. See ERAS below.

WHY THIS FILE EXISTS AT ALL

The official `mcp` Python SDK needs Python 3.10. The exercise floor is 3.9.
So this is written from the spec: no packages, no imports beyond the standard
library and the exercise's own mock backend. About four hundred lines, most of
them comments.

THE POINT OF THE EXERCISE

The two tools below are the SAME two functions the agent can already call in
process. Nothing about the model's job changes. What changes is who owns the
tool: a separate program, versioned on its own, reachable by any host. Watch
the token count before and after. It does not move. MCP fixes what you
maintain. It does nothing for routing.

ERAS

    modern (2026-07-28)   No handshake. Every request carries its protocol
                          version in params._meta. The server answers each
                          request on its own. `server/discover` is how a host
                          asks what the server supports.
    legacy (2025-11-25    An `initialize` request opens a session, the server
      and earlier)        answers with its protocolVersion, capabilities and
                          serverInfo, and the host follows up with a
                          `notifications/initialized` notification.

This file is dual-era: it serves whichever one the host opens with.

RUN IT

    python3 support/mcp_server.py --trace          # then type JSON at it
    python3 support/mcp_selftest.py                # the real check
    MCP_TRACE=1 python3 support/mcp_server.py

You will not normally run this by hand. A host runs it for you.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
EXERCISE_ROOT = HERE.parent
# Same resolution rule support/mock_backend.py uses: the data lives beside the
# code, two directories up from this file, so the server works from any cwd.
DATA_DIR = EXERCISE_ROOT / "data" / "americas"

if str(EXERCISE_ROOT) not in sys.path:
    sys.path.insert(0, str(EXERCISE_ROOT))

from support import mock_backend  # noqa: E402  (path has to be set first)

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
SERVER_NAME = "larkspur-ops"
SERVER_VERSION = "1.0.0"

# The revision this server was written against. Checked 6 Sep 2026 at
# https://modelcontextprotocol.io/specification/latest
PROTOCOL_MODERN = "2026-07-28"

# Handshake-based revisions. A host that opens with `initialize` gets one of
# these back, newest first.
PROTOCOL_LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

SUPPORTED_VERSIONS = [PROTOCOL_MODERN] + PROTOCOL_LEGACY

SERVER_INFO = {
    "name": SERVER_NAME,
    "title": "Larkspur Airlines ops lookups",
    "version": SERVER_VERSION,
}

CAPABILITIES = {"tools": {"listChanged": False}}

INSTRUCTIONS = (
    "Two read-only Larkspur Airlines lookups for disruption care. "
    "next_available_day answers questions about dates: the soonest day a "
    "stranded customer can actually fly. fare_rules returns the Handbook text "
    "behind an entitlement decision. Neither one holds, books, or pays for "
    "anything. Fixture data covers 7 to 9 May 2025 only."
)

# The per-request metadata keys the 2026-07-28 revision reserves.
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# JSON-RPC error codes. -32601 is the one a host uses to find out what a server
# cannot do, so it has to be exact.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022

TRACE = False
STRICT = False
TRACE_CHARS = 320
_WARNED_CAPS = False


# ---------------------------------------------------------------------------
# stderr only. Never stdout.
# ---------------------------------------------------------------------------
def _trace(arrow: str, payload: Any) -> None:
    """One compact line per message, so a human can watch the wire.

    `mcp →` is a line that came in on stdin. `mcp ←` is a line going out on
    stdout. Both go to stderr, which is the only channel a stdio server is
    allowed to talk to humans on.
    """
    if not TRACE:
        return
    text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    text = " ".join(text.split())
    if len(text) > TRACE_CHARS:
        text = text[: TRACE_CHARS - 1] + "…"
    sys.stderr.write("mcp %s %s\n" % (arrow, text))
    sys.stderr.flush()


def _note(text: str) -> None:
    """A remark for whoever is reading stderr. Always printed."""
    sys.stderr.write("mcp ! %s\n" % text)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Tool 1: next_available_day
#
# Wraps mock_backend.earliest_alternative_date, which was always there. Nobody
# had given a caller a way to reach it.
# ---------------------------------------------------------------------------
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def tool_next_available_day(origin: str, dest: str, date: str, cabin: str = "Y") -> Tuple[str, bool]:
    """Returns (text, is_error). Argument problems come back as tool errors,
    not protocol errors, because a model can fix those itself on the next try."""
    origin = (origin or "").strip().upper()
    dest = (dest or "").strip().upper()
    date = (date or "").strip()
    cabin = (cabin or "Y").strip().upper()

    if len(origin) != 3 or len(dest) != 3:
        return ("origin and dest must each be a three-letter airport code, "
                "for example DEN and BOI. Got %r and %r." % (origin, dest), True)
    if not ISO_DATE.match(date):
        return ("date must be ISO format, YYYY-MM-DD, for example 2025-05-08. "
                "Got %r." % date, True)
    if cabin not in ("Y", "J"):
        return ("cabin must be Y (main) or J (first). Got %r." % cabin, True)

    found = mock_backend.earliest_alternative_date(origin, dest, date, cabin)
    if not found:
        return ("No open seat from %s to %s in cabin %s on or after %s in the "
                "schedule this server can see. The fixture horizon is %s to %s."
                % (origin, dest, cabin, date,
                   mock_backend.DATA_HORIZON_START, mock_backend.DATA_HORIZON_END), False)
    return ("Earliest date with an open %s seat from %s to %s, searching forward "
            "from %s: %s. This answers for one passenger."
            % (cabin, origin, dest, date, found), False)


# ---------------------------------------------------------------------------
# Tool 2: fare_rules
#
# The Handbook text behind a policy row. Read straight off disk, sliced on the
# markdown headings, so "why won't you give me a hotel?" has a quotable answer.
# ---------------------------------------------------------------------------
FARE_RULES_PATH = DATA_DIR / "fare_rules_excerpt.md"
TRANSCRIPTS_PATH = DATA_DIR / "transcripts_sample.jsonl"

_sections_cache: Optional[List[Dict[str, str]]] = None


def _fare_rules_sections() -> List[Dict[str, str]]:
    """Split the excerpt on its `### ` headings. Cached: a classroom re-reads
    this a lot and the file never changes mid-session."""
    global _sections_cache
    if _sections_cache is not None:
        return _sections_cache

    sections: List[Dict[str, str]] = []
    try:
        text = FARE_RULES_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        _note("cannot read %s: %s" % (FARE_RULES_PATH, exc))
        _sections_cache = []
        return _sections_cache

    current: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        if line.startswith("### "):
            heading = line[4:].strip()
            match = re.match(r"^(\d+)\.\s*(.*)$", heading)
            current = {
                "number": match.group(1) if match else "",
                "title": match.group(2) if match else heading,
                "heading": heading,
                "lines": [],
            }
            sections.append(current)  # type: ignore[arg-type]
        elif current is not None:
            current["lines"].append(line)

    for section in sections:
        body = "\n".join(section.pop("lines")).strip()  # type: ignore[arg-type]
        section["text"] = "### %s\n\n%s" % (section["heading"], body)

    _sections_cache = sections  # type: ignore[assignment]
    return _sections_cache


def _section_labels() -> str:
    return ", ".join("%s (%s)" % (s["number"], s["title"]) for s in _fare_rules_sections())


def tool_fare_rules(section: str) -> Tuple[str, bool]:
    """Returns (text, is_error). Matches on the section number or on any part
    of its title, because a caller asking about hotels will not know it is
    section 6."""
    query = (section or "").strip().lower()
    query = re.sub(r"^section\s+", "", query).rstrip(".")
    if not query:
        return ("section is required. Available sections: %s." % _section_labels(), True)

    sections = _fare_rules_sections()
    if not sections:
        return ("The fare rules excerpt is not readable on this machine. Expected "
                "it at %s." % FARE_RULES_PATH, True)

    for candidate in sections:
        if query == candidate["number"]:
            return (candidate["text"], False)
    for candidate in sections:
        if query in candidate["title"].lower():
            return (candidate["text"], False)

    return ("No section matches %r. Available sections: %s."
            % (section, _section_labels()), True)


# ---------------------------------------------------------------------------
# Tool 3: reopen_stats
#
# Reads the transcripts JSONL corpus and computes, per intent label, how many
# contacts reopened within 72 hours and why. Agents call this before closing a
# case to understand which ticket types tend to come back—so the first reply
# covers what the customer will ask next.
# ---------------------------------------------------------------------------
def tool_reopen_stats(intent_label: str = "") -> Tuple[str, bool]:
    """Returns (text, is_error). Reads transcripts_sample.jsonl and aggregates
    reopen-within-72h stats, optionally filtered to one intent label."""
    label_filter = (intent_label or "").strip()

    try:
        records = []
        with open(TRANSCRIPTS_PATH, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if raw:
                    records.append(json.loads(raw))
    except OSError as exc:
        return ("Cannot read transcripts file at %s: %s" % (TRANSCRIPTS_PATH, exc), True)
    except json.JSONDecodeError as exc:
        return ("Malformed transcripts file: %s" % exc, True)

    # Accumulate per label: total, reopened count, list of reopen reasons.
    agg: Dict[str, Dict[str, Any]] = {}
    for record in records:
        label = record.get("intent_label") or ""
        if label_filter and label != label_filter:
            continue
        if label not in agg:
            agg[label] = {"total": 0, "reopened": 0, "reasons": []}
        agg[label]["total"] += 1
        if record.get("reopened_within_72h"):
            agg[label]["reopened"] += 1
            reason = (record.get("reopen_reason") or "").strip()
            if reason:
                agg[label]["reasons"].append(reason)

    if not agg:
        if label_filter:
            return ("No records found for intent_label %r in the transcripts corpus."
                    % label_filter, True)
        return ("No records found in the transcripts file.", True)

    # Sort by reopen rate descending.
    def _rate(item: tuple) -> float:
        data = item[1]
        return data["reopened"] / data["total"] if data["total"] else 0.0

    out_lines: List[str] = []
    if label_filter:
        out_lines.append("Reopen stats for intent_label=%r:" % label_filter)
    else:
        out_lines.append("Reopen stats for all intent labels (ranked by reopen rate):")

    for label, data in sorted(agg.items(), key=_rate, reverse=True):
        total = data["total"]
        reopened = data["reopened"]
        rate = reopened / total * 100 if total else 0.0
        top_reasons = [r for r, _ in Counter(data["reasons"]).most_common(3)]

        out_lines.append("")
        out_lines.append("intent_label: %s" % label)
        out_lines.append("  total contacts : %d" % total)
        out_lines.append("  reopened (72h) : %d" % reopened)
        out_lines.append("  reopen rate    : %.1f%%" % rate)
        if top_reasons:
            out_lines.append("  top reopen reasons:")
            for i, reason in enumerate(top_reasons, 1):
                out_lines.append("    %d. %s" % (i, reason))
        else:
            out_lines.append("  top reopen reasons: none")

    return ("\n".join(out_lines), False)


# ---------------------------------------------------------------------------
# Tool schemas
#
# This is the part a host actually reads. The description is the routing
# surface: the model cannot see the code above and cannot ask what a function
# does. Same bar as the nine tools in agent.py: when to call it, what it
# needs, what comes back, and the field descriptions route too.
# ---------------------------------------------------------------------------
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "next_available_day",
        "title": "Earliest open seat",
        "description": (
            "Answer the first question a cancelled or stranded Larkspur customer asks: "
            "what is the soonest day you can actually get me out? Call it for questions "
            "about DATES, when the customer wants to know how long they are stuck rather "
            "than which specific flight to take. It needs the departure and arrival "
            "airport codes, the date the customer was booked to travel, and the cabin. "
            "It searches Larkspur inventory forward from that date and returns the "
            "earliest date with an open seat as YYYY-MM-DD, or says plainly that there is "
            "no open seat in the schedule it can see. It holds nothing and books nothing, "
            "and it answers for a party of one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Departure airport, three-letter IATA code, e.g. DEN.",
                },
                "dest": {
                    "type": "string",
                    "description": "Arrival airport, three-letter IATA code, e.g. BOI.",
                },
                "date": {
                    "type": "string",
                    "description": ("The disrupted travel date in ISO format, YYYY-MM-DD, "
                                    "e.g. 2025-05-08. The search starts here and looks "
                                    "forward, never backward."),
                },
                "cabin": {
                    "type": "string",
                    "description": ("Cabin to search: Y for main, J for first. Use the "
                                    "cabin the customer is already ticketed in. Defaults "
                                    "to Y."),
                    "enum": ["Y", "J"],
                    "default": "Y",
                },
            },
            "required": ["origin", "dest", "date"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fare_rules",
        "title": "Handbook text",
        "description": (
            "Return the Larkspur Customer Commitment and fare rules text behind an "
            "entitlement decision, straight from the published Handbook excerpt. Call it "
            "when a customer challenges an answer and wants to know the rule, or asks "
            "why something is or is not covered, so the reply can quote the Handbook "
            "verbatim instead of paraphrasing it. Returns fare family rules including "
            "change fees, fare difference charges, and refundability per ticket type, "
            "as well as what is and is not covered per disruption cause. It needs one "
            "section: a number, or any words from the section title such as 'fare "
            "families', 'change fee', 'care while you wait'. It returns that section's "
            "full text as a verbatim Handbook excerpt. It is reference reading, not an "
            "entitlements decision: the policy table is still the only source of truth "
            "for what is owed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": ("Which section to read. A number like '6', or words "
                                    "from its title like 'fare families', 'when we delay', "
                                    "'care while you wait', 'chat automation'."),
                },
            },
            "required": ["section"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reopen_stats",
        "title": "Reopen rate by ticket type",
        "description": (
            "Look up how often a given ticket type (intent label) re-contacts within "
            "72 hours, from the transcripts corpus. Call this before closing a case to "
            "understand which ticket types are most likely to come back — so the first "
            "reply covers what the customer will ask next. Returns: contact count, reopen "
            "count, reopen rate %, and the most common reopen reasons. Pass intent_label "
            "to filter to one type; omit it to see all types ranked by reopen rate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent_label": {
                    "type": "string",
                    "description": ("The intent label to look up, e.g. "
                                    "'rebook_after_cancellation'. Omit to see all."),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "care_entitlements",
        "title": "Meal, hotel and goodwill entitlements",
        "description": (
            "Returns meal credit, hotel coverage, and goodwill eligibility for a disruption. "
            "Call when a customer asks what care Larkspur owes them while they wait. "
            "Pass cause_code, delay_minutes (0 for cancellations), and status. "
            "Appends Handbook section 6 text for verbatim quoting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cause_code": {
                    "type": "string",
                    "description": ("Disruption cause code from the flight status record. "
                                    "One of: WX (weather), ATC (air traffic control), "
                                    "MX (maintenance), CREW (crew availability), "
                                    "SEC (security)."),
                    "enum": ["WX", "ATC", "MX", "CREW", "SEC"],
                },
                "delay_minutes": {
                    "type": "integer",
                    "description": ("Current estimated departure delay in minutes. "
                                    "Pass 0 for a cancellation or diversion — "
                                    "the status field determines the policy band."),
                },
                "status": {
                    "type": "string",
                    "description": ("Current flight status. One of: DELAYED, CANCELLED, "
                                    "DIVERTED."),
                    "enum": ["DELAYED", "CANCELLED", "DIVERTED"],
                },
                "fare_family": {
                    "type": "string",
                    "description": ("Customer's fare family. One of: Basic, Main, "
                                    "Main Plus, First. Defaults to Main if omitted."),
                    "enum": ["Basic", "Main", "Main Plus", "First"],
                    "default": "Main",
                },
                "loyalty_tier": {
                    "type": "string",
                    "description": ("Customer's Skyline loyalty tier. One of: Member, "
                                    "Silver, Gold, Summit. Use Member when the PNR has "
                                    "no Skyline number. Defaults to Member if omitted."),
                    "enum": ["Member", "Silver", "Gold", "Summit"],
                    "default": "Member",
                },
            },
            "required": ["cause_code", "delay_minutes", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cause_in_plain_words",
        "title": "Plain-English disruption cause",
        "description": (
            "Translate a Larkspur disruption cause code into plain English the customer "
            "can understand and repeat. Call it when the customer asks why their flight "
            "was cancelled or delayed and you need a customer-facing phrase — not the raw "
            "code. Returns the customer-facing explanation from the policy handbook and "
            "states whether the cause is within or outside Larkspur's control, which "
            "determines care entitlements. Only needs the cause code; no other context "
            "is required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cause_code": {
                    "type": "string",
                    "description": ("Disruption cause code to explain. One of: WX (weather), "
                                    "ATC (air traffic control), MX (maintenance), "
                                    "CREW (crew availability), SEC (security)."),
                    "enum": ["WX", "ATC", "MX", "CREW", "SEC"],
                },
            },
            "required": ["cause_code"],
            "additionalProperties": False,
        },
    },
]

# ---------------------------------------------------------------------------
# Tool 4: care_entitlements
#
# Answers "do I get a meal or hotel while I wait?" by consulting the
# disruption_policy.json table and appending the Handbook section 6 text.
# ---------------------------------------------------------------------------
DISRUPTION_POLICY_PATH = DATA_DIR / "disruption_policy.json"

_policy_cache: Optional[Dict[str, Any]] = None


def _load_policy() -> Optional[Dict[str, Any]]:
    """Load and cache disruption_policy.json. Returns None on read error."""
    global _policy_cache
    if _policy_cache is not None:
        return _policy_cache
    try:
        _policy_cache = json.loads(DISRUPTION_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _note("cannot read %s: %s" % (DISRUPTION_POLICY_PATH, exc))
        return None
    return _policy_cache


def _band_for(status: str, delay_minutes: int,
              delay_bands: List[Dict[str, Any]]) -> Optional[str]:
    """Map (status, delay_minutes) to a delay band code."""
    if status == "CANCELLED":
        return "CXL"
    if status == "DIVERTED":
        return "DIV"
    for bd in delay_bands:
        if status not in bd.get("applies_to_status", []):
            continue
        lo = bd.get("min_minutes", 0)
        hi = bd.get("max_minutes")  # None = no upper limit
        if delay_minutes >= lo and (hi is None or delay_minutes <= hi):
            return bd["band"]
    return None


def tool_care_entitlements(
    cause_code: str,
    delay_minutes: int,
    status: str,
    fare_family: str = "Main",
    loyalty_tier: str = "Member",
) -> Tuple[str, bool]:
    """Returns (text, is_error). Summarises meal, hotel and goodwill
    entitlements by looking up the policy table and appending section 6."""
    VALID_CAUSES = ("WX", "ATC", "MX", "CREW", "SEC")
    VALID_STATUSES = ("DELAYED", "CANCELLED", "DIVERTED")
    TIER_ORDER = ["Member", "Silver", "Gold", "Summit"]

    cause_code = (cause_code or "").strip().upper()
    status = (status or "").strip().upper()
    fare_family = (fare_family or "Main").strip()
    loyalty_tier = (loyalty_tier or "Member").strip()

    if cause_code not in VALID_CAUSES:
        return ("cause_code must be one of %s. Got %r."
                % (", ".join(VALID_CAUSES), cause_code), True)
    if status not in VALID_STATUSES:
        return ("status must be one of %s. Got %r."
                % (", ".join(VALID_STATUSES), status), True)
    if not isinstance(delay_minutes, int):
        return ("delay_minutes must be an integer. Got %r." % delay_minutes, True)

    policy = _load_policy()
    if policy is None:
        return ("Cannot read disruption policy at %s." % DISRUPTION_POLICY_PATH, True)

    cause_class = policy.get("cause_classes", {}).get(cause_code)
    if not cause_class:
        return ("Unknown cause_code: %r." % cause_code, True)

    band = _band_for(status, delay_minutes, policy.get("delay_bands", []))
    if band is None:
        return ("Could not determine delay band for status=%r, delay_minutes=%d."
                % (status, delay_minutes), True)

    # Find the matching policy row.
    row = next(
        (r for r in policy.get("rows", [])
         if r.get("cause_class") == cause_class and r.get("band") == band),
        None,
    )
    if row is None:
        return ("No policy row found for cause_class=%r, band=%r."
                % (cause_class, band), True)

    care = row.get("care", {})
    goodwill_data = row.get("goodwill", {})
    row_id = row.get("policy_row_id", "?")

    # --- Meal ---------------------------------------------------------------
    meal_usd_raw = care.get("meal_usd", 0)
    meal_base = 0
    meal_conditional = False
    meal_threshold = None
    if isinstance(meal_usd_raw, int):
        meal_base = meal_usd_raw
    elif isinstance(meal_usd_raw, dict):
        meal_base = meal_usd_raw.get("amount", 0)
        meal_threshold = meal_usd_raw.get("if_wait_minutes_for_alternative_gte")
        meal_conditional = True

    # Apply Gold/Summit meal bonus.
    tier_rules = policy.get("loyalty_rules", {}).get(loyalty_tier, {})
    meal_bonus = tier_rules.get("meal_bonus_usd", 0)
    meal_bonus_cap = tier_rules.get("meal_bonus_cap_usd")
    meal_final = meal_base
    if meal_base > 0 and meal_bonus > 0:
        total = meal_base + meal_bonus
        meal_final = min(total, meal_bonus_cap) if meal_bonus_cap else total

    # --- Hotel --------------------------------------------------------------
    hotel = care.get("hotel", {})
    hotel_eligible = hotel.get("eligible", False)
    hotel_cap = hotel.get("nightly_cap_usd", 175)
    hotel_note = hotel.get("instead", "")

    # Summit courtesy hotel override (uncontrollable rows only).
    summit_hotel_override = False
    if loyalty_tier == "Summit" and cause_class == "uncontrollable":
        summit_overrides = (policy.get("loyalty_rules", {})
                            .get("Summit", {})
                            .get("courtesy_overrides", {}))
        unc_hotel_ov = summit_overrides.get("uncontrollable_overnight_hotel", {})
        if row_id in unc_hotel_ov.get("applies_to_rows", []):
            summit_hotel_override = True

    # --- Goodwill -----------------------------------------------------------
    goodwill_eligible = bool(goodwill_data.get("eligible", False))
    goodwill_cap = 0
    goodwill_source = ""
    if goodwill_eligible:
        gw_rules = policy.get("goodwill_rules", {})
        base = policy.get("goodwill_base_by_tier_usd", {}).get(loyalty_tier, 0)
        adj = gw_rules.get("fare_family_adjustment_usd", {}).get(fare_family, 0)
        extra = gw_rules.get("b4_extra_usd", 0) if band == "B4" else 0
        # Basic fare is only goodwill-eligible from Silver upward.
        if fare_family == "Basic":
            min_tier = gw_rules.get("basic_fare_minimum_tier", "Silver")
            if TIER_ORDER.index(loyalty_tier) < TIER_ORDER.index(min_tier):
                goodwill_eligible = False
                goodwill_cap = 0
            else:
                goodwill_cap = base + adj + extra
        else:
            goodwill_cap = base + adj + extra
        goodwill_source = "policy"

    # Summit courtesy goodwill override (uncontrollable rows only).
    summit_goodwill = False
    if not goodwill_eligible and loyalty_tier == "Summit" and cause_class == "uncontrollable":
        summit_overrides = (policy.get("loyalty_rules", {})
                            .get("Summit", {})
                            .get("courtesy_overrides", {}))
        unc_gw_ov = summit_overrides.get("uncontrollable_goodwill", {})
        if row_id in unc_gw_ov.get("applies_to_rows", []):
            summit_goodwill = True
            goodwill_cap = unc_gw_ov.get("cap_usd", 50)
            goodwill_source = "Summit courtesy"

    # --- Build summary text -------------------------------------------------
    lines: List[str] = []
    lines.append("CARE ENTITLEMENTS SUMMARY")
    lines.append("Policy row : %s" % row_id)
    lines.append("Cause      : %s (%s)" % (cause_code, cause_class))
    lines.append("Status     : %s  Delay: %d min  Band: %s" % (status, delay_minutes, band))
    lines.append("Fare family: %s  Loyalty: %s" % (fare_family, loyalty_tier))
    lines.append("")

    # Meal line.
    if meal_conditional:
        if meal_final > 0:
            lines.append("Meal credit : $%d — applies once wait for alternative >= %d min "
                         "(threshold not yet reached or unknown)"
                         % (meal_final, meal_threshold or 0))
        else:
            lines.append("Meal credit : none")
    elif meal_final > 0:
        bonus_note = (" (includes $%d %s tier bonus)" % (meal_bonus, loyalty_tier)
                      if meal_bonus > 0 else "")
        lines.append("Meal credit : $%d%s" % (meal_final, bonus_note))
    else:
        lines.append("Meal credit : none")

    # Hotel line.
    if hotel_eligible is True:
        lines.append("Hotel       : covered — up to $%d/night, 1 night, "
                     "requires agent approval" % hotel_cap)
    elif hotel_eligible == "if_overnight":
        lines.append("Hotel       : covered if overnight away from home airport "
                     "— up to $%d/night, 1 night, requires agent approval" % hotel_cap)
    else:
        lines.append("Hotel       : not covered")
        if hotel_note:
            lines.append("  (%s)" % hotel_note)
        if summit_hotel_override:
            lines.append("  Summit courtesy: hotel may be offered if overnight "
                         "(up to $175/night, requires agent approval)")

    # Goodwill line.
    if goodwill_eligible:
        if goodwill_cap <= 75:
            approval = "auto-issuable"
        elif goodwill_cap <= 200:
            approval = "requires agent approval"
        else:
            approval = "requires supervisor approval"
        lines.append("Goodwill    : eligible — up to $%d travel credit (%s)"
                     % (goodwill_cap, approval))
    elif summit_goodwill:
        lines.append("Goodwill    : Summit courtesy — up to $%d travel credit "
                     "(always requires agent approval)" % goodwill_cap)
    else:
        lines.append("Goodwill    : not eligible")

    lines.append("")
    lines.append("Customer explainer:")
    lines.append(row.get("explainer_en", ""))

    # Append Handbook section 6 for reference.
    sec6 = next((s for s in _fare_rules_sections() if s.get("number") == "6"), None)
    if sec6:
        lines.append("")
        lines.append("--- Handbook Section 6 (Care while you wait) ---")
        lines.append(sec6.get("text", ""))

    return ("\n".join(lines), False)


# ---------------------------------------------------------------------------
# Tool 5: cause_in_plain_words
#
# Answers "why was my flight cancelled, in words I can repeat?" by returning
# the customer-facing label from the policy JSON plus the control/uncontrollable
# distinction that determines care eligibility.
# ---------------------------------------------------------------------------
_CAUSE_FALLBACK = {
    "WX":   "weather conditions outside the airline's control",
    "ATC":  "air traffic control restrictions outside the airline's control",
    "MX":   "aircraft maintenance — this was within our control",
    "CREW": "crew availability — this was within our control",
    "SEC":  "a security event at the airport, outside the airline's control",
}


def tool_cause_in_plain_words(cause_code: str) -> Tuple[str, bool]:
    """Returns (text, is_error). Translates a cause code into customer-facing
    language and states whether it is within or outside Larkspur's control."""
    VALID_CAUSES = ("WX", "ATC", "MX", "CREW", "SEC")

    cause_code = (cause_code or "").strip().upper()
    if cause_code not in VALID_CAUSES:
        return ("cause_code must be one of %s. Got %r."
                % (", ".join(VALID_CAUSES), cause_code), True)

    policy = _load_policy()
    # Prefer the customer label from the policy JSON (field cause_labels_customer).
    customer_label = ""
    if policy:
        label_data = policy.get("cause_labels_customer", {}).get(cause_code, {})
        customer_label = label_data.get("en", "") if isinstance(label_data, dict) else ""

    # Fall back to the built-in mapping if the JSON field is missing or empty.
    if not customer_label:
        customer_label = _CAUSE_FALLBACK.get(cause_code, cause_code)

    cause_classes = (policy or {}).get("cause_classes", {})
    cause_class = cause_classes.get(cause_code, "uncontrollable")
    is_controllable = cause_class == "controllable"
    control_phrase = ("within Larkspur's control" if is_controllable
                      else "outside Larkspur's control")

    lines: List[str] = []
    lines.append("Cause code          : %s" % cause_code)
    lines.append("Customer explanation: %s" % customer_label)
    lines.append("Control             : %s" % control_phrase)
    lines.append("")
    if is_controllable:
        lines.append("Because this is within our control, the customer may be eligible for:")
        lines.append("  - Meal credit (delay >= 2 h: $15 or more; cancellation: up to $25)")
        lines.append("  - Hotel if overnight away from home airport")
        lines.append("  - Goodwill travel credit (delays >= 3 h, cancellations, diversions)")
    else:
        lines.append("Because this is outside our control, care is more limited:")
        lines.append("  - Meal credit (delay >= 3 h: $15; cancellation/diversion after >= 3 h wait: $15)")
        lines.append("  - Hotel: not covered (distressed-traveler rate link offered instead)")
        lines.append("  - Goodwill: not eligible (Summit courtesy up to $50 at agent discretion)")

    return ("\n".join(lines), False)


TOOL_IMPLS = {
    "next_available_day": tool_next_available_day,
    "fare_rules": tool_fare_rules,
    "reopen_stats": tool_reopen_stats,
    "care_entitlements": tool_care_entitlements,
    "cause_in_plain_words": tool_cause_in_plain_words,
}


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------
class RpcError(Exception):
    """A protocol-level failure: bad method, bad params, unsupported version.
    Distinct from a tool that ran and did not like its arguments. That comes
    back as a normal result with isError set."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _result(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Every result carries resultType and the server's identity. A host on an
    older revision ignores both; the 2026-07-28 revision expects them."""
    out = {"resultType": "complete"}
    out.update(payload)
    meta = dict(out.get("_meta") or {})
    meta[META_SERVER_INFO] = SERVER_INFO
    out["_meta"] = meta
    return out


def _check_modern_meta(meta: Dict[str, Any]) -> None:
    """The 2026-07-28 revision requires protocolVersion and clientCapabilities
    on every single request. There is no session to remember them in."""
    version = meta.get(META_VERSION)
    if version not in SUPPORTED_VERSIONS:
        raise RpcError(UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
                       {"supported": SUPPORTED_VERSIONS, "requested": version})
    if META_CLIENT_CAPS not in meta:
        if STRICT:
            raise RpcError(INVALID_PARAMS,
                           "Missing required _meta field %s" % META_CLIENT_CAPS)
        global _WARNED_CAPS
        if not _WARNED_CAPS:
            _WARNED_CAPS = True
            _note("this host sends no %s; the spec requires it. Serving anyway. "
                  "Run with --strict to refuse." % META_CLIENT_CAPS)


def dispatch(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """One method in, one result out. Raises RpcError for anything the
    protocol, rather than a tool, has an opinion about."""
    if method == "server/discover":
        # How a modern host asks a server to introduce itself.
        return _result({
            "supportedVersions": SUPPORTED_VERSIONS,
            "capabilities": CAPABILITIES,
            "instructions": INSTRUCTIONS,
        })

    if method == "initialize":
        # The handshake older hosts open with. Negotiation rule from the spec:
        # answer with the client's version if we speak it, otherwise with the
        # newest one we do.
        asked = params.get("protocolVersion")
        if asked in PROTOCOL_LEGACY or asked == PROTOCOL_MODERN:
            agreed = asked
        else:
            agreed = PROTOCOL_LEGACY[0]
            _note("client asked for protocolVersion %r; answering with %s"
                  % (asked, agreed))
        client = params.get("clientInfo") or {}
        if TRACE:
            _note("initialize from %s %s" % (client.get("name", "unknown host"),
                                             client.get("version", "")))
        return _result({
            "protocolVersion": agreed,
            "capabilities": CAPABILITIES,
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })

    if method == "ping":
        return _result({})

    if method == "tools/list":
        return _result({"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")
        if name not in TOOL_IMPLS:
            # Unknown tool is a protocol error, not a tool error: the model
            # cannot fix a tool that does not exist.
            raise RpcError(INVALID_PARAMS, "Unknown tool: %s" % name,
                           {"available": sorted(TOOL_IMPLS)})
        try:
            text, is_error = TOOL_IMPLS[name](**arguments)
        except TypeError as exc:
            # Wrong or missing arguments. The model CAN fix this, so it goes
            # back as a tool error with the schema's own words in it.
            text, is_error = ("Bad arguments for %s: %s. Check the tool's "
                              "inputSchema." % (name, exc)), True
        except Exception as exc:  # noqa: BLE001 - a tool crash is a tool result
            text, is_error = "%s failed: %s: %s" % (name, type(exc).__name__, exc), True
        return _result({"content": [{"type": "text", "text": text}], "isError": is_error})

    raise RpcError(METHOD_NOT_FOUND, "Method not found: %s" % method)


def process(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Turn one parsed message into one response, or None for a notification.

    A notification has no id and never gets an answer. That is the whole rule,
    and it is why `notifications/initialized` produces silence rather than an
    empty result.
    """
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": INVALID_REQUEST, "message": "Not a JSON-RPC 2.0 message"}}

    method = message.get("method")
    params = message.get("params")
    if params is None:
        params = {}
    has_id = "id" in message and message["id"] is not None

    if not has_id:
        # Notifications. `notifications/initialized` closes the legacy
        # handshake; `notifications/cancelled` gives up on an in-flight call.
        # Neither gets a reply.
        if method == "notifications/initialized" and TRACE:
            _note("handshake complete")
        return None

    request_id = message["id"]
    try:
        if not isinstance(method, str):
            raise RpcError(INVALID_REQUEST, "method must be a string")
        meta = (params.get("_meta") or {}) if isinstance(params, dict) else {}
        if META_VERSION in meta:
            _check_modern_meta(meta)
        result = dispatch(method, params if isinstance(params, dict) else {})
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except RpcError as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}
    except Exception as exc:  # noqa: BLE001 - a crash must not kill the server
        _note("internal error on %s: %s: %s" % (method, type(exc).__name__, exc))
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": INTERNAL_ERROR,
                          "message": "%s: %s" % (type(exc).__name__, exc)}}


def _write(obj: Dict[str, Any]) -> None:
    line = json.dumps(obj, separators=(",", ":"), default=str)
    _trace("←", line)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def serve(stdin=None, stdout=None) -> int:
    """Read one JSON object per line until stdin closes, answer each one.

    Closing stdin is how a host says goodbye, so end of file means exit, not
    error.
    """
    stream = stdin if stdin is not None else sys.stdin
    for raw in stream:
        line = raw.strip()
        if not line:
            continue
        _trace("→", line)
        try:
            message = json.loads(line)
        except ValueError as exc:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": PARSE_ERROR, "message": "Parse error: %s" % exc}})
            continue
        response = process(message)
        if response is not None:
            _write(response)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    global TRACE, STRICT
    parser = argparse.ArgumentParser(
        description="Larkspur MCP server (stdio, standard library only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--trace", action="store_true",
                        help="echo every request and response line to stderr "
                             "(or set MCP_TRACE=1)")
    parser.add_argument("--strict", action="store_true",
                        help="reject requests missing a required _meta field, "
                             "as the 2026-07-28 revision says to (or MCP_STRICT=1)")
    parser.add_argument("--version", action="store_true",
                        help="print the protocol revisions this server speaks and exit")
    args = parser.parse_args(argv)

    TRACE = args.trace or os.environ.get("MCP_TRACE") == "1"
    STRICT = args.strict or os.environ.get("MCP_STRICT") == "1"

    if args.version:
        # stdout is fine here: --version is not a protocol session.
        print("%s %s  speaks: %s" % (SERVER_NAME, SERVER_VERSION,
                                     ", ".join(SUPPORTED_VERSIONS)))
        return 0

    if TRACE:
        _note("%s %s ready on stdio, %d tool(s): %s"
              % (SERVER_NAME, SERVER_VERSION, len(TOOLS),
                 ", ".join(t["name"] for t in TOOLS)))
    try:
        return serve()
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        # The host went away mid-write. Nothing to report to.
        return 0


if __name__ == "__main__":
    sys.exit(main())
