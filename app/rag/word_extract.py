"""Trigger-word extraction: after the AI refused a request, name the single words that made it unfit for the exercise app.
In this test it is a separate small AI call (same model as V3). In the final V3 it becomes one extra field in the brain's reply, so no extra call.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

MODEL = "openai/gpt-4.1-mini"

SYSTEM = """You are a safety filter for a rehab and exercise app that only handles joint, knee and back exercise conditions. A patient wrote a short complaint.
Decide whether it names a problem the exercise app must NOT handle, and if so name the trigger words.

Problem types:
- emergency: urgent or general-illness symptoms (fever, cold, cough, vomiting, breathing trouble, chest pain, dizziness, numbness, tingling, paralysis, weakness, burning, infection...)
- out_of_scope: skin, stomach or digestive, eye, ear, and other conditions the exercise app does not cover (acne, itching, constipation, acidity, stomach, eyes, ulcer, scabies...)
- pregnancy: pregnancy or periods related (LMP, pregnant, amenorrhea, period, discharge...)

Reply with JSON only: {"refuse": true or false, "label": "emergency" or "out_of_scope" or "pregnancy" or "none", "trigger_words": [..]}
Rules for trigger_words:
- Return EVERY single word that, ON ITS OWN, names such a problem, not just the first one. Copy each word exactly as the patient wrote it (keep their spelling mistakes).
- NEVER return normal exercise-complaint words: pain, ache, back, knee, joint, waist, neck, stiffness, or numbers, durations and filler words.
- If no single word names the problem (for example only "3 Days", "Not recorded", an abbreviation, or the meaning needs several words together), return an empty list.
- A plain knee, back or joint complaint is not a problem: refuse false, label none, empty list.
- If words belong to different problem types, use the label of the most serious one (emergency, then pregnancy, then out_of_scope)."""


def extract(question: str, retries: int = 2, api_key: str | None = None) -> dict:
    """{'label': str, 'words': [str], 'cost': float}. Never raises: on any failure it returns no words (the request was already refused anyway)."""
    body = json.dumps({"model": MODEL, "temperature": 0, "max_tokens": 80, "usage": {"include": True},
                       "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]}).encode()
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body, method="POST",
                                         headers={"Authorization": "Bearer " + (api_key or os.environ["OPENROUTER_API_KEY"]), "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            m = re.search(r"\{.*\}", d["choices"][0]["message"]["content"], re.S)
            j = json.loads(m.group(0)) if m else {}
            words = [w for w in (j.get("trigger_words") or []) if isinstance(w, str)]
            return {"label": j.get("label") or "none", "words": words if j.get("refuse") else [], "cost": (d.get("usage") or {}).get("cost", 0.0) or 0.0}
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
            time.sleep(1 + attempt)
    return {"label": "none", "words": [], "cost": 0.0}
