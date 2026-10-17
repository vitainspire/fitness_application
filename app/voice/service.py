import json
import re
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.catalog.models import ActivityTaxonomy
from app.platform.errors import QuotaExhausted, RateLimited
from app.rag.service import _consume_quota, _refund_quota, VOICE_QUOTA_TYPE, VOICE_DAILY_LIMIT
from app.rag.vectorstore import chat_complete, VOICE_CHAT_MODEL, UpstreamUnavailable, ResponseTruncated

# A written distance in the transcript: "3 km", "2.5 kilometers", "5k", "3 miles", "800 meters".
# Bare "m" is deliberately not matched (it could mean minutes), nor "mi".
_DISTANCE_NUMBER = re.compile(
    r"(\d+(?:\.\d+)?)\s*(km|kms|kilomet(?:er|re)s?|miles?|met(?:er|re)s?)\b|(\d+(?:\.\d+)?)\s*k\b",
    re.IGNORECASE)
# Any distance word at all, with or without a readable number ("two kilometers", "half a mile").
_DISTANCE_WORD = re.compile(
    r"\b(?:km|kms|kilomet(?:er|re)s?|miles?|met(?:er|re)s?)\b|\b\d+(?:\.\d+)?\s*k\b", re.IGNORECASE)


def _spoken_distances_km(transcript: str) -> list[float]:
    """Every distance written as a number in the transcript, in kilometers, in the order
    spoken. Used only as a safety net: the model is asked to report the distance itself,
    but sometimes leaves it empty when a time is also mentioned."""
    out = []
    for m in _DISTANCE_NUMBER.finditer(transcript or ""):
        if m.group(1) is not None:
            value, unit = float(m.group(1)), m.group(2).lower()
            km = value * 1.609344 if unit.startswith("mile") else value / 1000 if unit.startswith("met") else value
        else:
            km = float(m.group(3))
        km = round(km, 2)
        if km > 0:
            out.append(km)
    return out


def _positive_numbers(value) -> list[float]:
    if not isinstance(value, list):
        return []
    return [s for s in value if isinstance(s, (int, float)) and not isinstance(s, bool) and s > 0]


def _positive_minutes(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def extract_activities(session: Session, user_id: str, transcript: str, *, openrouter_api_key: str) -> dict:
    if not _consume_quota(session, user_id, quota_type=VOICE_QUOTA_TYPE, limit=VOICE_DAILY_LIMIT):
        raise QuotaExhausted(
            "You've used your voice logging attempts for today — try again tomorrow, "
            "or log this one manually."
        )
    session.commit()

    rows = session.execute(
        select(ActivityTaxonomy.activity_type, ActivityTaxonomy.display_name,
               ActivityTaxonomy.synonyms, ActivityTaxonomy.amount_unit,
               ActivityTaxonomy.prescription_type, ActivityTaxonomy.est_pace_min_per_km)
    ).all()

    valid_codes = set()
    taxonomy_list = []
    # Average pace (minutes per km) of each distance exercise, from the taxonomy —
    # used to turn "walked 30 minutes" into a distance.
    pace_by_code = {}
    for code, name, synonyms, amount_unit, prescription_type, pace in rows:
        valid_codes.add(code)
        if prescription_type == "distance" and pace:
            pace_by_code[code] = float(pace)
        syn_str = " | ".join(synonyms) if synonyms else "none"
        taxonomy_list.append(f"- code: {code}, name: {name}, unit: {amount_unit}, synonyms: {syn_str}")
    
    taxonomy_str = "\n".join(taxonomy_list)

    prompt = f"""You are Vita, a highly advanced, encouraging personal fitness AI assistant. The user spoke their workout log and it was transcribed by a speech-to-text model, which may have introduced errors (mishearing exercise names, adding filler words, etc.).

Your job:
1. Read the transcript and identify every distinct exercise/activity mentioned.
2. Map each one to the closest match in the taxonomy below (using the code, name, OR synonyms).
3. Extract the sets and reps/distance for each.
4. Use your general fitness knowledge to correct obvious speech-to-text errors and match each spoken activity to the best taxonomy entry.
5. If an exercise genuinely cannot be identified even after correction, set is_custom: true and use your best guess for activity_name (clean English name, not the garbled transcript).
6. Write a short, natural, encouraging Vita-style reply to the user in reply_text — acknowledge what they did, like a confident AI companion would.

Taxonomy (map the spoken word to the closest code — fuzzy matching is OK):
{taxonomy_str}

Output Schema (JSON object — single pass, no extra API calls):
{{
  "reply_text": "A short, conversational Vita-style response. E.g. 'Excellent work on those 10 pushups, sir. Take a moment to catch your breath, you have earned it.'",
  "entries": [
    {{
      "activity_type": "exact_code_from_taxonomy_or_null",
      "activity_name": "clean exercise name if not in taxonomy, else null",
      "is_custom": false,
      "set_amounts": [number, number, ...],
      "unit": "the canonical unit, per the UNITS rules below",
      "duration_minutes": null
    }}
  ]
}}

Rules:
- reply_text must be short (1-2 sentences), warm, and direct — speak to the user as Vita would.
- entries must contain all extracted exercises.
- set_amounts is an array where EACH item represents ONE set's reps or distance. 
- CRITICAL: If the transcript specifies multiple sets, you MUST output an array containing the reps repeated for each set (e.g., "X sets of Y reps" becomes [Y, Y, ... repeated X times]). Do NOT just extract the set count.
- Even a single set must be in a list.
- Exception — a DISTANCE exercise (its unit above is km) where the user gave only a time and no distance (e.g. "walked for 30 minutes"): output set_amounts as an empty list [] and put the minutes in duration_minutes. Do NOT guess a distance; the app converts the time itself. If the user gave a distance, use it as normal.
- If the user said BOTH a distance and a time (for example "walked 3 km in 40 minutes"), ALWAYS put the spoken distance in set_amounts (here [3]) and the time in duration_minutes. Never leave set_amounts empty when a distance was spoken.
- activity_type must be an exact taxonomy code from the list above, or null.
- Never output garbled/nonsensical text from the transcript in activity_name. Always clean it up.
- Never invent reps or sets that were not mentioned.
- One array item per distinct exercise.

UNITS — CRITICAL. Always output amounts in the canonical unit, never in the unit the user happened to speak:
- Each taxonomy entry above lists its own unit. For a taxonomy match, set "unit" to exactly that unit and convert every value in set_amounts into it.
- Distance is always kilometers ("km"): meters divided by 1000 (3000 meters becomes 3), miles multiplied by 1.609, kilometers unchanged. Round to 2 decimals. Never output meters or miles.
- Time: for a taxonomy match use that entry's own unit (for a hold measured in "sec", 1 minute becomes 60). For a custom exercise, time is "min": seconds divided by 60, hours multiplied by 60.
- Repetition counts use "count" as the "unit" value (this exact word is how the app saves them). In reply_text, always call them "reps", never "count".
- In reply_text, whenever you mention an amount, include its unit word: reps for counts, km for distance, minutes for time, seconds for holds (for example "15 reps", "2 km", "45 minutes"). This applies to the spoken reply only — the "unit" field keeps its canonical value.
- For an exercise NOT in the taxonomy (is_custom: true), choose the unit from what the user measured: a distance gives "km", a time gives "min", anything else gives "count". Decide from the measurement, not from the exercise's name, so cycling, swimming, rowing or anything else behave the same way.
- If the user gives a number in the canonical unit already (for example "3 km"), keep it as is.
- Examples: "3000 meters of cycling" becomes a custom entry with unit "km" and set_amounts [3]. "swam for 45 minutes" becomes a custom entry with unit "min" and [45]. "walked 800 meters" becomes unit "km" and [0.8]. "15 reps" becomes unit "count" and [15].

Transcript: "{transcript}"
Respond ONLY with the JSON object, no explanation, no markdown.
"""

    messages = [{"role": "user", "content": prompt}]

    try:
        raw_response = chat_complete(messages, openrouter_api_key, model=VOICE_CHAT_MODEL)
    except (UpstreamUnavailable, ResponseTruncated):
        _refund_quota(session, user_id, quota_type=VOICE_QUOTA_TYPE)
        session.commit()
        raise RateLimited(
            "Voice extraction is busy right now. Please try again in a moment — "
            "this attempt was not counted against your daily limit.",
            retry_after=30,
        )

    try:
        raw_response = raw_response.strip()
        if raw_response.startswith("```"):
            raw_response = raw_response.strip("`")
            if raw_response[:4].lower() == "json":
                raw_response = raw_response[4:]
        print(f"RAW LLM RESPONSE: {raw_response}", flush=True)
        data = json.loads(raw_response)

        # Fallback: LLM returned the old array format instead of the new object format
        if isinstance(data, list):
            data = {"entries": data, "reply_text": "Got it!"}
        
        if not isinstance(data, dict):
            return {"entries": [], "transcript": transcript, "reply_text": "Got it!"}

        entries_raw = data.get("entries", [])
        if not isinstance(entries_raw, list):
            entries_raw = []
        reply_text = data.get("reply_text", "Got it!")

        # Safety net for a time-AND-distance phrase ("walked 3 km in 40 minutes") where the
        # model left the amount empty: work out which spoken distances no entry already
        # carries, and how many distance entries are waiting for one.
        spoken = _spoken_distances_km(transcript)
        mentions_distance = bool(_DISTANCE_WORD.search(transcript or ""))
        remaining = list(spoken)
        for it in entries_raw:
            if isinstance(it, dict):
                for used in _positive_numbers(it.get("set_amounts")):
                    for idx, d in enumerate(remaining):
                        if abs(d - used) < 0.011:
                            del remaining[idx]
                            break
        needy_count = sum(
            1 for it in entries_raw
            if isinstance(it, dict) and pace_by_code.get(it.get("activity_type"))
            and not _positive_numbers(it.get("set_amounts")) and _positive_minutes(it.get("duration_minutes")))

        entries = []
        for item in entries_raw:
            if not isinstance(item, dict):
                continue
                
            code = item.get("activity_type")
            if code is not None and code not in valid_codes:
                item["activity_type"] = None
                item["is_custom"] = True
                item["activity_name"] = code
                
            sets = item.get("set_amounts")
            has_amount = isinstance(sets, list) and any(
                isinstance(s, (int, float)) and not isinstance(s, bool) and s > 0 for s in sets)
            minutes = item.get("duration_minutes")
            pace = pace_by_code.get(item.get("activity_type"))
            if not has_amount and pace and _positive_minutes(minutes):
                if needy_count == 1 and len(remaining) == 1:
                    # The user SAID a distance (and a time): keep the distance, never the time.
                    item["set_amounts"] = [remaining[0]]
                    item["unit"] = "km"
                    sets = item["set_amounts"]
                elif remaining or (mentions_distance and not spoken):
                    # A distance was said but can't be tied to this entry safely (several
                    # entries, or a number written in words): don't invent one from the time.
                    pass
                else:
                    # Time only: distance = time / average pace (done here, not by the model).
                    km = round(minutes / pace, 2)
                    if km > 0:
                        item["set_amounts"] = [km]
                        item["unit"] = "km"
                        item["estimated_from_minutes"] = minutes  # lets the screen mark it as an estimate
                        sets = item["set_amounts"]
            if not isinstance(sets, list) or len(sets) == 0:
                continue
            
            valid_sets = []
            for s in sets:
                if isinstance(s, (int, float)) and not isinstance(s, bool) and s > 0:
                    valid_sets.append(s)
            
            if not valid_sets:
                continue
            item["set_amounts"] = valid_sets
            entries.append(item)
            
        return {"entries": entries, "transcript": transcript, "reply_text": reply_text}

    except json.JSONDecodeError:
        return {"entries": [], "transcript": transcript, "reply_text": "Got it!"}
