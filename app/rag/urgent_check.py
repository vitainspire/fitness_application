"""Urgent-symptom screen: code rules (no AI) and a tiny YES/NO AI check. Ported from the tested prototype (testing_temparary/variants/v2.py + v3.py).

Owner's rule: this app never tells a user to go to a hospital or call an emergency number. An urgent hit means "consult a doctor" (see chat_messages).
One deliberate change from the diagram: if the AI answers badly twice, the result is 'unavailable' (neutral note added), NOT 'yes'. A garbled reply must not
show an alarm to an ordinary user; the code rules below have already run.
"""
from __future__ import annotations

import logging
import os
import re

from app.rag import vectorstore
from app.rag.vectorstore import ResponseTruncated, UpstreamUnavailable

_log = logging.getLogger(__name__)
URGENT_MODEL = os.environ.get("ROUTER_MODEL", "openai/gpt-4.1-mini")      # override only for tests (for example a free model); the default is the production model

URGENT_RULES = [('bladder_bowel', "\\b(lost|loss of|cannot control|can'?t control|no control|unable to control|leak\\w*)\\W+(?:\\w+\\W+){0,3}(bladder|bowel|urine|stool)"), ('bladder_bowel_hi', '\\b(peshab|pishab|potty|latrine|motion)\\W+(?:\\w+\\W+){0,4}(control|leak|ruk|nahi ho|nhi ho)'), ('leg_numb', "\\b(cannot|can'?t|unable to|no)\\W+(?:\\w+\\W+){0,2}(feel|move)\\W+(?:\\w+\\W+){0,3}(leg|legs|foot|feet)"), ('leg_numb2', '\\b(numbness|numb)\\b.{0,25}\\b(between (my )?(legs|thighs)|groin|saddle)'), ('leg_numb_hi', '\\b(pair|pao|paon|taang|taangein|tang)\\W+(?:\\w+\\W+){0,3}(sunn|sun ho|sunn ho|senseless|numb)'), ('chest', '\\b(chest|seene|seena|chhati|chaati)\\W+(?:\\w+\\W+){0,4}(pain|tight\\w*|pressure|dard)'), ('chest2', '\\b(pain|dard)\\W+(?:\\w+\\W+){0,3}(in|mein|me)\\W+(?:\\w+\\W+){0,2}(chest|seene|seena)'), ('breath', "\\b(cannot|can'?t|unable to|difficulty|trouble)\\W+(?:\\w+\\W+){0,2}(breathe|breathing)|\\bsaans\\W+(?:\\w+\\W+){0,3}(nahi|phool|ruk)"), ('faint', '\\b(fainted|passed out|unconscious|behosh)\\b'), ('cant_bear_weight', "\\b(cannot|can'?t|unable to)\\W+(?:\\w+\\W+){0,3}(walk|stand|put (any )?weight|bear weight)|\\bchal nahi paa"), ('broken', '\\b(bone (is |looks )?broken|broke my|fracture[d]?|haddi toot|deformed|looks deformed)\\b'), ('sudden_severe', '\\b(sudden(ly)? (and )?(severe|terrible|unbearable)|worst pain of my life|achanak (bahut )?(tez|zyada) dard)\\b')]
_RULES = [(n, re.compile(p, re.I)) for n, p in URGENT_RULES]
_FEVER = re.compile('\\b(fever|bukhar|bukhaar|shivering|chills|kaanp\\w*)\\b', re.I)
_HOTJOINT = re.compile('\\b(red|lal|hot|garam|swollen|swelling|sujan|sooj\\w*)\\b', re.I)
_NEG = re.compile('\\b(no|not|without|never|nahi hai)\\W+(?:\\w+\\W+){0,2}$', re.I)
_TASK = "You are a safety screener for a health app. Read the user's message (it may be English, Hindi, Hinglish or misspelt). Answer YES only if it describes a possible medical EMERGENCY that needs in-person care today. YES examples: loss of bladder or bowel control; numbness between the legs or in both legs together with back pain; sudden weakness or inability to move a limb; chest pain or pressure; trouble breathing; fainting; a hot, red, swollen joint TOGETHER WITH fever or chills; cannot put any weight on a limb after an injury, a heard pop, a visible deformity or a suspected broken bone; sudden, severe, unbearable pain; signs of infection after an injury. thoughts of suicide or self-harm, wanting to die or not wanting to live, hopelessness with a wish to end their life, or taking too many pills (an overdose). Answer NO for ordinary, ongoing or mild complaints, even if uncomfortable: stiffness, aching, tingling or numb fingers or hands (for example at night or while typing or driving), pain that shoots down one leg, a burning or sore toe without fever, difficulty with daily tasks, general questions about a condition, or asking when to see a doctor. When the message mixes a mild complaint with one of the YES examples, answer YES."
_P1 = 'You are a safety screener for a health app. Read the user\'s message (it may be English, Hindi, Hinglish or misspelt). Answer YES only if it describes a possible medical EMERGENCY that needs in-person care today. YES examples: loss of bladder or bowel control; numbness between the legs or in both legs together with back pain; sudden weakness or inability to move a limb; chest pain or pressure; trouble breathing; fainting; a hot, red, swollen joint TOGETHER WITH fever or chills; cannot put any weight on a limb after an injury, a heard pop, a visible deformity or a suspected broken bone; sudden, severe, unbearable pain; signs of infection after an injury. thoughts of suicide or self-harm, wanting to die or not wanting to live, hopelessness with a wish to end their life, or taking too many pills (an overdose). Answer NO for ordinary, ongoing or mild complaints, even if uncomfortable: stiffness, aching, tingling or numb fingers or hands (for example at night or while typing or driving), pain that shoots down one leg, a burning or sore toe without fever, difficulty with daily tasks, general questions about a condition, or asking when to see a doctor. When the message mixes a mild complaint with one of the YES examples, answer YES.\nReply with exactly one word: YES or NO.\n\nUser message:\n"""{msg}"""'
_P2 = 'Your previous reply was not valid. You must reply with exactly one word and nothing else: YES or NO. Do not explain.\n\nYou are a safety screener for a health app. Read the user\'s message (it may be English, Hindi, Hinglish or misspelt). Answer YES only if it describes a possible medical EMERGENCY that needs in-person care today. YES examples: loss of bladder or bowel control; numbness between the legs or in both legs together with back pain; sudden weakness or inability to move a limb; chest pain or pressure; trouble breathing; fainting; a hot, red, swollen joint TOGETHER WITH fever or chills; cannot put any weight on a limb after an injury, a heard pop, a visible deformity or a suspected broken bone; sudden, severe, unbearable pain; signs of infection after an injury. thoughts of suicide or self-harm, wanting to die or not wanting to live, hopelessness with a wish to end their life, or taking too many pills (an overdose). Answer NO for ordinary, ongoing or mild complaints, even if uncomfortable: stiffness, aching, tingling or numb fingers or hands (for example at night or while typing or driving), pain that shoots down one leg, a burning or sore toe without fever, difficulty with daily tasks, general questions about a condition, or asking when to see a doctor. When the message mixes a mild complaint with one of the YES examples, answer YES.\n\nUser message:\n"""{msg}"""'
_VALID = re.compile('\\s*(yes|no)\\W*\\s*$', re.I)


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s&']", " ", t.lower())).strip()


def urgent_by_rules(text: str) -> str | None:
    """Name of the urgent-word rule that fired, or None. Pure code, no AI."""
    t = _norm(text)
    for name, rx in _RULES:
        for m in rx.finditer(t):
            if not _NEG.search(t[: m.start()]):
                return name
    fever = [m for m in _FEVER.finditer(t) if not _NEG.search(t[: m.start()])]
    if fever and _HOTJOINT.search(t):
        return "fever_hot_joint"
    return None


def urgent_by_ai(text: str, api_key: str) -> str:
    """'yes' | 'no' | 'unavailable'."""
    msg = text[:500]
    for tmpl in (_P1, _P2):
        reply = None
        for provider_try in (1, 2):
            try:
                reply = vectorstore.chat_complete([{"role": "user", "content": tmpl.format(msg=msg)}], api_key, model=URGENT_MODEL,
                                                  retries=1, temperature=0, max_tokens=3)
                break
            except ResponseTruncated:
                reply = ""
                break
            except UpstreamUnavailable as e:
                if "empty content" in str(e):
                    reply = ""
                    break
                reply = None
                if provider_try == 2:
                    _log.warning("urgent AI check unavailable: %s", e)
                    return "unavailable"
            except RuntimeError as e:
                _log.warning("urgent AI check failed: %s", e)
                return "unavailable"
        m = _VALID.match(reply or "")
        if m:
            return m.group(1).lower()
    return "unavailable"
