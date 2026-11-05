"""Pipeline V2 pieces (NEW_PIPELINE_V2=true; needs NEW_CHAT_PIPELINE=true and NEW_ROUTER=true). Pure code, no database, no AI:
  - free refusals: self-harm words, instruction-like text ("system:", "ignore previous instructions", ...)
  - the "add this exercise" route: matching the exercise the user named, add / remove verbs, and the fixed messages
  - the code checks that confirm what the router suggests (condition words, "more" words, add verbs, targets words, "tell me more" questions)
  - the fixed messages for "I am healthy now", data the app does not track, a condition the app has no exercises for, and "remove it from chat"
  - "other ideas" (activities outside the approved list): parsing the model's hidden line, filtering it, and the fixed doctor-first block
  - cutting an unapproved exercise out of an answer instead of throwing the whole answer away
Principle: the router only SUGGESTS; these checks CONFIRM. Undo: NEW_PIPELINE_V2=false (the router and the rest keep working as before).
"""
from __future__ import annotations

import os
import re


def enabled() -> bool:
    on = lambda k: os.getenv(k, "false").strip().lower() == "true"  # noqa: E731
    return on("NEW_CHAT_PIPELINE") and on("NEW_ROUTER") and on("NEW_PIPELINE_V2")


# ---------------------------------------------------------------------------------------------------------------- self-harm words
# Needs a clear intent. "I might hurt myself doing squats" / "I hurt myself at the gym" are about injury, not self-harm, and are NOT matched.
_SELF_HARM = re.compile(
    r"(suicid\w*|self[- ]?harm|kill(ing)? (my ?self|me)\b|end(ing)? (it all|my life|this life|everything)\b|(give|giving|gave) up on (life|living)|"
    r"want(ed)? to die|wish i (was|were) dead|better off dead|(don'?t|do not|dont|no longer) want to (live|be alive|be here|exist)|not worth living|"
    r"life (is|isn'?t|is not|isnt) worth|no reason to live|no point (in )?living|"
    r"(took|taken|swallowed|take|taking) (too many|a lot of|all my|all the) (pills|tablets|medicines?|medication)|overdos\w*|"
    r"(want to|wanna|going to|gonna|plan to|planning to|am going to|decided to|ready to) (hurt|harm|cut|injure|kill) (my ?self)|"
    r"(hurt|harm|kill|cut) (my ?self|me) if (you|u)\b|cutting my ?self|cut my ?self (on purpose|deliberately)|"
    r"mar jaunga|mar jaungi|marna chahta|marna chahti|jaan de dunga|jaan de dungi|apni jaan|jeena nahi|jeene ka mann nahi|khatam kar (lunga|lungi)|zindagi se thak|"
    r"आत्महत्या|खुदकुशी|मरना चाहत|जीना नहीं|जान दे)", re.I)


_NEG_SELF = re.compile(r"(don'?t|do not|dont|never|no longer|won'?t|wouldn'?t|not going to)\s+(ever |really )?(want to |going to |plan to )?(end (it all|my life)|kill (my ?self|me)|die|hurt (my ?self))", re.I)


def self_harm(text: str) -> bool:
    t = re.sub(r"\s+", " ", text or "")
    if _NEG_SELF.search(t):
        return False                                    # "i dont want to end it all"
    return bool(_SELF_HARM.search(t))


# ---------------------------------------------------------------------------------------------------------------- instruction-like text (free refusal, never reaches a model)
# Only phrases that are instructions TO THE APP. Plain words like "ignore" or "print" alone (e.g. "can I ignore this pain?", "print my progress") are not matched.
_INSTRUCTION = re.compile(
    r"((^|\s)system\s*:|</?\s*(system|assistant|instructions?)\s*>|\bignore (all |the |any |your |my )?(previous|prior|above|earlier)\b|"
    r"\bignore (all |the |any |your )?(instructions|rules|restrictions)\b|\b(disregard|forget|override|bypass) (all |the |any |your |previous )*(instructions|rules|restrictions|safety)\b|"
    r"\bnew rules? ?(:|from|for you|for the (app|bot|assistant))|\bfrom now on you\b|\byou are now\b|\b(admin|developer|dan|god|jailbreak) mode\b|\bjailbreak\b|"
    r"\b(print|reveal|show|repeat|output|tell me|quote|translate|what are) (me )?(your|the) (system )?(prompt|instructions|rules|guidelines)\b|\brepeat after me\b|\bappend the line\b|"
    r"suggested_exercises|other_ideas|\bpretend (you are|to be)\b|\bact as (a |an )?(doctor|dan|unrestricted)|\bdeveloper:)", re.I)


def looks_like_instruction(text: str) -> bool:
    return bool(_INSTRUCTION.search(text or ""))


# ---------------------------------------------------------------------------------------------------------------- fixed messages
HEALTHY_MESSAGE = ("I can't suggest exercises outside the rules for your saved health condition ({labels}). If you really don't have {it} any more, tap the profile icon, "
                   "choose Edit, and remove it from your health conditions, and then I can suggest more. Please remove it only if you have no pain, for your own safety.")
UNSUPPORTED_MESSAGE = ("The app doesn't track that. It tracks your exercises, sets, reps, time and distance. I can show what you did on a day, or how your exercises are going "
                       "- for example, ask: what did I do yesterday?")
REMOVE_MESSAGE = ("I can't remove exercises from chat. You can remove it yourself: open the exercise player, go to that exercise, and tap the red Remove icon.")
NOT_IN_TABLE_MESSAGE = ("This could be {label}. I don't have verified exercises for it yet, so I can't suggest exercises for it. Please speak to a doctor about it. "
                        "If you do have {label}, you can add it yourself in your profile (profile icon, Edit).")
ASK_WHICH_EXERCISE = "Which exercise would you like to add? I can add: {names}."
AMBIGUOUS_EXERCISE = "Which one do you mean: {names}?"
SWITCH_HINT = " Say \"undo my changes\" to get the paused exercises back."

_UNTRACKED = re.compile(r"\b(calor\w*|kcal|heart ?rate|pulse|heartbeat|dhadkan|weight|wajan|vajan|bmi|blood pressure|bp|blood sugar|sleep|neend|steps|diet|food|protein|vo2|oxygen|spo2)\b", re.I)


def mentions_untracked(text: str) -> bool:
    """The router's 'not tracked' flag is only honoured when the message really names data the app does not keep."""
    return bool(_UNTRACKED.search(text or ""))


def _join_names(labels: list[str]) -> str:
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]


def healthy_message(labels: list[str]) -> str:
    return HEALTHY_MESSAGE.format(labels=_join_names(labels), it="it" if len(labels) == 1 else "any of them")


def not_in_table_message(label: str) -> str:
    return NOT_IN_TABLE_MESSAGE.format(label=label)


GATE_REASONS_WITH_REMOVE_TIP = ("not_in_exercise_table", "no_table_row", "out_of_scope")


def gate_message_named(reason: str, labels: list[str], fallback: str) -> str:
    """The doctor message for a saved condition set with no exercise list, NAMING the saved conditions and saying how to remove one that is not really there.
    The remove tip is only for table gaps (a condition outside the 12, or a combination with no row): never for pregnancy, heart, surgery or injury."""
    if reason not in GATE_REASONS_WITH_REMOVE_TIP or not labels:
        return fallback
    one = len(labels) == 1
    names = _join_names(labels)
    return (f"I can share general information, but I can't suggest exercises while {names} {'is' if one else 'are'} saved in your settings. If you don't really have "
            f"{'it' if one else 'one of them'}, tap the profile icon, choose Edit, and remove {'it' if one else 'it'} from your health conditions, and then I can suggest exercises. "
            f"Please remove only what you don't have, for your own safety. If you do have {'it' if one else 'them'}, please ask your doctor which exercises are safe for you.")


# One-line plain-language descriptions shown under the "which condition fits?" button. DRAFT wording: a doctor should review it before real users see it.
CONDITION_DESCRIPTIONS = {
    "chronic_back_pain": "Long-lasting pain in the lower or upper back.",
    "herniated_disk": "A disc in the spine that bulges and can press on a nerve, often causing back or leg pain.",
    "degenerative_disc": "Wear of the discs between the spine bones with age, causing back stiffness or pain.",
    "spinal_stenosis": "Narrowing of the space in the spine, which can cause back pain and leg numbness.",
    "spondylosis": "Age-related wear of the neck or back bones, causing stiffness and pain.",
    "arthritis_hip": "Joint pain or stiffness in the hip.",
    "arthritis_knee": "Joint pain or stiffness in the knee.",
    "bursitis": "Swelling of a small fluid sac next to a joint, mostly the shoulder, elbow or hip.",
    "carpal_tunnel": "Tingling or numbness in the hand or fingers from a pinched nerve at the wrist.",
    "sprain_strain": "A pulled muscle or twisted ligament after an injury.",
    "gout": "Sudden, painful joint swelling, often the big toe, linked to uric acid.",
    "prediabetes": "Blood sugar higher than normal but not yet diabetes.",
    "diabetes_controlled": "Diabetes that is kept under control with treatment.",
    "bp_controlled": "High blood pressure that is kept under control with treatment.",
    "high_cholesterol": "Too much fat (cholesterol) in the blood.",
    "obesity": "Body weight well above the healthy range.",
    "depression": "Long-lasting low mood or loss of interest in things.",
    "anxiety": "Frequent worry, fear or nervousness.",
    "pcos": "A hormone condition in women that can cause irregular periods.",
}

# A body-area or topic word that must be in the message before the router's "which condition fits?" suggestion is kept (a wrong tap SAVES the condition).
_COND_KEYWORDS = {
    "chronic_back_pain": r"back|kamar|pith|spine|spinal",
    "herniated_disk": r"dis[ck]|slip|sciatica|back|leg pain",
    "degenerative_disc": r"dis[ck]|degenerat|back|spine",
    "spinal_stenosis": r"stenosis|spine|spinal|back|leg (pain|numb\w*|weak\w*)",
    "spondylosis": r"spondyl\w*|neck|spine|spinal|cervical|back",
    "arthritis_hip": r"hips?|kulh\w*|groin",
    "arthritis_knee": r"knees?|ghutn\w*",
    "bursitis": r"bursitis|elbow|shoulder|hip point",
    "carpal_tunnel": r"wrist|carpal|hands?|fingers?|tingl\w*|numb\w*",
    "sprain_strain": r"sprain\w*|strain\w*|twist\w*|pulled|ankle|muscle|torn",
    "gout": r"gout|uric|toe|big toe",
    "prediabetes": r"sugar|prediabet\w*|diabet\w*|glucose",
    "diabetes_controlled": r"sugar|diabet\w*|glucose",
    "bp_controlled": r"blood pressure|\bbp\b|hypertension",
    "high_cholesterol": r"cholesterol|lipid|triglycerid\w*",
    "obesity": r"obes\w*|overweight|weight|bmi|fat",
    "depression": r"depress\w*|sad|low mood|mood|hopeless",
    "anxiety": r"anxi\w*|worr\w*|panic|stress\w*|nervous",
    "pcos": r"pcos|pcod|periods?|menstrua\w*",
}
_COND_RX = {c: re.compile(rf"\b({p})\b", re.I) for c, p in _COND_KEYWORDS.items()}


def condition_supported_by_words(code: str, text: str) -> bool:
    """True when the message contains a word for this condition's body area or topic (or the condition has no word list)."""
    rx = _COND_RX.get(code)
    return True if rx is None else bool(rx.search(text or ""))


# A second opinion for the router's "which condition fits?" decision: the router (an AI call) sometimes answers NONE for a clear body-area message when another condition is
# already saved. These words are checked in code only when the router said NONE for a medical message, and only for conditions the user has NOT saved.
_CONDITION_WORDS = [
    ("chronic_back_pain", re.compile(r"\b(lower back|upper back|back (pain|hurts?|ache|aches|stiff\w*|spasm)|backache|kamar (me|mein)? ?dard|kamar dard|pith (me|mein)? ?dard)\b", re.I)),
    ("arthritis_hip", re.compile(r"\b(hip (pain|hurts?|ache|aches|stiff\w*)|my hips? (hurts?|ache|aches|pain)|kulhe (me|mein)? ?dard)\b", re.I)),
]


def condition_from_words(text: str, option_codes: set[str]) -> str | None:
    for code, rx in _CONDITION_WORDS:
        if code in option_codes and rx.search(text or ""):
            return code
    return None


# ---------------------------------------------------------------------------------------------------------------- checks that confirm what the router suggests
_MORE = re.compile(r"\b(besides|in addition|additional|extra|what else|anything else|something else|other (exercises?|activities|options|ideas|stretches)|another|"
                   r"different (exercises?|activities|options|ideas)|aur (exercises?|options|ideas|activities|kuch|kya|batao|bataiye|bhi)|iske alawa|ke alawa|alawa)\b|\bmore (exercises?|options|ideas|activities|stretches|movements|workouts?)\b|"
                   r"\b(give|show|suggest|want|need|any|some)( me)? more\b(?! (about|info|information|details?))", re.I)
_ADD = re.compile(r"\b(add|put|include|insert|resume|restore|enable|activate|turn\b.{0,40}\bon|jod(o| do| de)?|daalo|shamil)\b|"
                  r"\b(in|to|into|on) my (plan|program|routine|list|dashboard)\b", re.I)
_REMOVE = re.compile(r"\b(remove|delete|take (it |this |that )?out|hatao|hata do|nikal do|nikalo)\b", re.I)
_NOT_PLAN = re.compile(r"\b(account|profile|chat|history|data|message|conversation|password)\b", re.I)
_PLAN_WORDS = re.compile(r"\b(plan|program|routine|list|exercises?|workout|dashboard)\b", re.I)
_INFO_Q = re.compile(r"^\s*(tell me more|tell me about|more about|explain|describe|how does|why (does|do|is))\b", re.I)
_NOT_TARGETS = re.compile(r"\b(rest|break|between|technique|posture|breath\w*|warm.?up|cool.?down|form|safely|how to|why)\b", re.I)
_TARGET_WORDS = re.compile(r"\b(target|reps?|how many|kitni|kitne|baar)\b", re.I)
_REC = re.compile(r"\b(try|start with|begin with|suggest\w*|recommend\w*|consider|can do|could do|should do|include|add|focus on|work on|go for|aim for|good (choice|option|start))\b", re.I)


_MED_ACTION = re.compile(r"\b(stop|skip|quit|miss|increase|decrease|change|reduce|double|replace|switch)\b.{0,25}\b(medicines?|medications?|tablets?|pills?|dose|dosage|drugs?|syrup|injections?|steroids?|painkillers?|antibiotics?)\b", re.I)


_TARGETISH = re.compile(r"\b(targets?|goals?|reps?|sets?|how many|how long|how far|kitni|kitne|kitna|baar|distance|duration|minutes?|seconds?|km|kilomet\w*)\b|\d", re.I)
_HEALTHY_WORDS = re.compile(r"\b(healthy|no pain|pain[- ]?free|fully recovered|recovered|cured|fine now|completely fine|all better|no longer|any ?more|forget|ignore my|pretend|"
                            r"remove (my|it)|condition (is )?(gone|none)|don'?t have)\b", re.I)


def asks_for_more(text: str) -> bool:
    """The 'add' mode (Add button, other ideas) is only used when the message really asks for MORE or DIFFERENT exercises."""
    return bool(_MORE.search(text or ""))


_ADD_HI = re.compile(r"\b(daal(\s?(do|de|na|o))?|dal(\s?(do|de|o))?|jod(\s?(do|de|na))?|jodo|lagao|add\s?k(ar|r)(\s?(do|de))?)\b|(डाल|जोड़|जोड|शामिल)")
_REMOVE_HI = re.compile(r"\b(hata(\s?(do|de|o))?|hatao|nikal(\s?(do|de))?|nikalo)\b|(हटा|निकाल)")


def has_add_verb(text: str) -> bool:
    return bool(_ADD.search(text or "") or _ADD_HI.search(text or ""))


def asks_to_remove(text: str, has_exercise: bool = False) -> bool:
    """A remove verb together with the plan / an exercise ("remove walking from my plan"). A health question like "how to remove knee swelling" is not matched."""
    return bool(_REMOVE.search(text or "") or _REMOVE_HI.search(text or "")) and not _NOT_PLAN.search(text or "") and (has_exercise or bool(_PLAN_WORDS.search(text or "")))


def is_info_question(text: str) -> bool:
    """'tell me more', 'explain', 'how does ...': never a request for an exercise list."""
    return bool(_INFO_Q.search(text or ""))


def not_a_targets_question(text: str) -> bool:
    """Rest time, technique, posture, breathing ...: normal answers, not the user's reps/sets targets."""
    return bool(_NOT_TARGETS.search(text or "")) and not _TARGET_WORDS.search(text or "")


def is_recommendation(answer: str) -> bool:
    return bool(_REC.search(answer or ""))


def apply_router_checks(question: str, rr: dict, option_codes: set[str], in_table: set[str], previous_answer: str = "") -> dict:
    """The code checks on the router's record (see the module docstring). Returns a corrected copy; never calls an AI."""
    g = dict(rr)
    g["add_mode"] = "add" if asks_for_more(question) else "switch"
    if g["intent"] in ("program_targets", "medical", "performance", "conversation", "add_exercise") and asks_to_set_target(question):
        g["intent"] = "set_target"                       # the chat cannot change a target: a fixed message says how
    elif g["intent"] in ("program_targets", "medical", "performance", "conversation") and asks_increase_target(question):
        g["intent"] = "increase_target"                  # answered from the user's real adherence numbers, with the confirm chip when eligible
    if g["intent"] == "add_exercise":
        if asks_to_remove(question, bool(g["exercise"])):
            g["intent"] = "remove_exercise"
        elif is_choice_followup(question) and (previous_answer or "").startswith(CHOICE_PREFIXES):
            g["exercise"] = ""                            # "both" answers OUR question "Which one do you mean: A, B?": the app reads the names from its previous answer
        elif not has_add_verb(question) and (g["exercise"] or g["sport"] or not previous_answer):     # no name at all ("yes please" after our suggestion) = add those, read from the previous answer
            g["intent"] = "medical"                      # "exercises for swimming please" is a request for advice, not a command to add
        elif not g["exercise"] and g["sport"]:
            g["exercise"] = g["sport"]                    # "put swimming in my plan": the router put the name in the wrong field
    elif g["intent"] in ("medical", "conversation") and asks_to_remove(question, False) and g["exercise"]:
        g["intent"] = "remove_exercise"
    elif g["intent"] == "medical" and g["exercise"] and has_add_verb(question) and not g["wants_exercises"]:
        g["intent"] = "add_exercise"
    if g["intent"] == "program_targets" and (not_a_targets_question(question) or not _TARGETISH.search(question)):
        g["intent"] = "medical"                          # a targets answer needs a targets word: "Which exercises should I start with?" has none
    if g["intent"] == "conversation" and looks_like_instruction(question):
        g["intent"] = "medical"
    if g["intent"] in ("activity_recall", "performance", "medical") and asks_what_assistant_said(question):
        g["intent"] = "conversation"                     # "what did you suggest earlier?" is answered from the chat, not from the workout log
    if is_info_question(question) or is_yes_no_question(question):
        g["wants_exercises"] = False                       # an explanation or a yes/no question never gets an add / switch chip
    if _MED_ACTION.search(question):
        g["wants_exercises"] = False                       # "can I stop my BP medicine and do yoga?" is a medicine question for a doctor: no exercise chip
    if g["condition"] and not condition_supported_by_words(g["condition"], question):
        g["condition"] = None                              # a wrong tap SAVES a condition: keep the router's guess only if the message has a word for it
    if g["intent"] == "medical" and not g["condition"] and not g["claims_healthy"]:
        g["condition"] = condition_from_words(question, option_codes)
    g["unsupported_data"] = bool(g["unsupported_data"] and mentions_untracked(question))
    if g["claims_healthy"] and not _HEALTHY_WORDS.search(question):
        g["claims_healthy"] = False                      # "my doctor said it is fine, add running" is an authority claim, not "I am healthy now"
    return g


# ---------------------------------------------------------------------------------------------------------------- the add-exercise route
def _list(names: list[str]) -> str:
    return ", ".join(names)


def not_in_app_message(raw: str, approved: list[str]) -> str:
    tail = f" Exercises I can add for you: {_list(approved)}." if approved else ""
    return (f"I can't add \"{raw.strip()[:60]}\" to your program because it isn't one of the exercises the app has checked for your condition. "
            f"If you really want to try it, please speak to your doctor first. You can also log it yourself as a custom exercise.{tail}")


def blocked_protocol(verb: str, display_name: str, labels: list[str] | None, approved: list[str]) -> str:
    """THE message for an exercise that exists in the app but is blocked for the user's saved condition(s): it will not be suggested or added; if a doctor says the problem is solved
    the condition can be removed in the profile (only then), or the user can log the exercise themselves. Same wording on every route (add, plan, routine, sport question)."""
    tail = (f" Exercises I can add for you: {_list(approved)}." if verb == "add" else f" What I can suggest for you: {_list(approved)}.") if approved else ""
    if not labels:
        return f"I can't {verb} {display_name} for your condition. If you really want to try it, please speak to your doctor first.{tail}"
    one = len(labels) == 1
    return (f"I can't {verb} {display_name} for your condition ({_join_names(labels)}). If your doctor confirms {'that problem is' if one else 'those problems are'} solved, tap the profile icon, "
            f"choose Edit, and remove {'it' if one else 'the one that no longer applies'}, but only if it is really gone. You can also log {display_name} yourself as a custom exercise; I can't suggest it.{tail}")


def not_approved_message(display_name: str, approved: list[str], labels: list[str] | None = None) -> str:
    return blocked_protocol("add", display_name, labels, approved)


def add_message(display_names: list[str], resume: list[str], already: list[str]) -> str:
    parts = []
    new = [n for n in display_names if n not in resume]
    if new:
        parts.append(f"{_list(new)} {'is' if len(new) == 1 else 'are'} approved for you. Tap the button below to add {'it' if len(new) == 1 else 'them'} to your program (nothing else changes).")
    if resume:
        parts.append(f"{_list(resume)} {'is' if len(resume) == 1 else 'are'} paused in your program. Tap the button below to turn {'it' if len(resume) == 1 else 'them'} back on.")
    if already:
        parts.append(f"{_list(already)} {'is' if len(already) == 1 else 'are'} already in your program.")
    return " ".join(parts)


def add_caution(modify_names: list[str], own: dict[str, str], general: list[str]) -> str:
    """The caution line for exercises the table marks `modify` (allowed, with care): each one's own caution when the table has it, else the row's general cautions."""
    text = f" Use {_list(modify_names)} with caution for your health conditions."
    own_lines = [f"{n}: {own[n]}" for n in modify_names if own.get(n)]
    if own_lines:
        text += " " + " ".join(own_lines)
    if general and any(not own.get(n) for n in modify_names):
        text += " " + " ".join(general[:2])
    return text


def _norm_word(w: str) -> str:
    w = w.lower()
    if len(w) > 3 and w.endswith("s"):
        w = w[:-1]
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    return w


_DEVA_EX = {"वॉकिंग": "walking", "चलना": "walking", "ग्लूट ब्रिज": "glute bridge", "डेड बग": "dead bug", "शोल्डर सर्कल": "shoulder circles", "रनिंग": "running", "दौड़ना": "running",
            "एंकल सर्कल": "ankle circles", "रिस्ट सर्कल": "wrist circles", "वॉल काफ स्ट्रेच": "wall calf stretch", "हैमस्ट्रिंग स्ट्रेच": "seated hamstring stretch",
            "थाई स्ट्रेच": "side-lying thigh stretch", "नेक होल्ड": "neck hold", "साइड स्ट्रेच": "seated side stretch"}


def to_latin(text: str) -> str:
    """Devanagari spellings of the 12 exercise names -> English, so 'शोल्डर सर्कल जोड़ दो' can be matched like 'shoulder circles'."""
    t = text or ""
    for hi, en in _DEVA_EX.items():
        t = t.replace(hi, " " + en + " ")
    return t


def norm(text: str) -> str:
    return " ".join(_norm_word(w) for w in re.findall(r"[a-z]+", to_latin(text).lower()))


def match_exercises(text: str, catalog: dict[str, dict]) -> list[str]:
    """activity_types of the catalog exercises `text` names. An exact name (plurals ignored) gives just that one; otherwise EVERY exercise whose name contains the words
    (or is contained in them), so an ambiguous word like "stretch" or "circles" returns several and the caller asks which one is meant.
    catalog: activity_type -> {"name": display name, "synonyms": [..]}"""
    t = norm(text)
    if not t:
        return []
    partial = []
    for at, info in catalog.items():
        names = {info["name"], at.replace("_", " "), *(info.get("synonyms") or [])}
        hit = False
        for n in names:
            nn = norm(n)
            if not nn:
                continue
            if nn == t:
                return [at]
            if re.search(rf"\b{re.escape(nn)}\b", t) or re.search(rf"\b{re.escape(t)}\b", nn):
                hit = True
        if hit:
            partial.append(at)
    return partial


def match_exercise(text: str, catalog: dict[str, dict]) -> str | None:
    found = match_exercises(text, catalog)
    return found[0] if found else None


def mentioned(text: str, display_name: str) -> bool:
    return re.search(rf"\b{re.escape(display_name.lower())}\b", (text or "").lower()) is not None


# ---------------------------------------------------------------------------------------------------------------- other ideas
OTHER_IDEAS_RULE = ("\n\nOTHER IDEAS: after your answer, on its own final line, write OTHER_IDEAS: followed by up to 3 OTHER low-impact activities (NOT on the approved list, "
                    "no medicines) that could also help with the user's goal or sport, for example OTHER_IDEAS: cycling, swimming. Only activities that do not load the painful "
                    "body area. If there are none, write OTHER_IDEAS: NONE. Do not describe these activities in your answer text.")
_IDEAS_LINE = re.compile(r"^[ \t]*OTHER_IDEAS:[ \t]*(.*)$", re.I | re.M)
_IMPACT = ["jump", "plyo", "sprint", "burpee", "hiit", "box jump"]
_BLOCK = {
    "knee_hip": ["run", "jog", "lunge", "squat", "step-up", "step up", "stair", "kneel", "football", "soccer", "basketball", "cricket", "badminton", "tennis"],
    "back": ["deadlift", "sit-up", "sit up", "crunch", "toe touch", "twist", "kettlebell", "heavy", "bench press", "overhead", "rowing", "golf"],
    "weight": ["run", "jog"],
    "heart_sugar": ["heavy", "max effort", "powerlift"],
}
_AREA = {"arthritis_knee": "knee_hip", "arthritis_hip": "knee_hip", "chronic_back_pain": "back", "herniated_disk": "back", "degenerative_disc": "back",
         "obesity": "weight", "bp_controlled": "heart_sugar", "diabetes_controlled": "heart_sugar", "prediabetes": "heart_sugar", "high_cholesterol": "heart_sugar"}
OTHER_IDEAS_BLOCK = ("Other ideas (not checked for your condition): {ideas}. Please get your doctor's OK before trying any of these. "
                     "You can log them yourself as a custom exercise in the app.")


def split_other_ideas(raw: str) -> tuple[str, list[str]]:
    """(raw text without the OTHER_IDEAS line, the ideas the model listed)."""
    m = _IDEAS_LINE.search(raw or "")
    if not m:
        return raw, []
    text = (raw[:m.start()] + raw[m.end():]).rstrip()
    ideas = [i.strip(" .\t") for i in re.split(r"[,;]", m.group(1)) if i.strip(" .\t")]
    return text, [] if [i.lower() for i in ideas] == ["none"] else ideas


def _blocked(low: str, block: list[str]) -> bool:
    """A blocklist term matches at the START of a word ("run" blocks "running", not "trunk")."""
    return any(re.search(rf"(?<![a-z]){re.escape(b)}", low) for b in block)


def filter_ideas(ideas: list[str], *, approved_names: list[str], avoided_names: list[str], catalog_names: list[str], condition_codes: list[str]) -> list[str]:
    """Drop duplicates, anything already approved, any app exercise (approved ones are shown as such, the others are not offered as ideas) and (first cut, needs
    clinical review) activities that load the affected area. Activities outside the app's 12 exercises cannot be checked against the safety grid: they only get this blocklist,
    the model's own rule, and the fixed doctor-first sentence."""
    block = list(_IMPACT)
    for c in condition_codes:
        block += _BLOCK.get(_AREA.get(c, ""), [])
    out, seen = [], set()
    approved_l = {a.lower() for a in approved_names}
    catalog_l = {c.lower() for c in catalog_names}
    for idea in ideas:
        low = idea.lower()
        if not (2 <= len(low) <= 40) or low in seen or low in approved_l or low in catalog_l or _blocked(low, block):
            continue
        seen.add(low)
        out.append(idea)
        if len(out) == 3:
            break
    return out


def other_ideas_block(ideas: list[str]) -> str:
    return OTHER_IDEAS_BLOCK.format(ideas=_list(ideas)) if ideas else ""


# ---------------------------------------------------------------------------------------------------------------- cut an unapproved exercise instead of refusing
def cut_unapproved(answer: str, catalog_names: list[str], approved: list[str]) -> tuple[str, list[str]]:
    """(answer without the sentences that name an unapproved app exercise, the unapproved names found). Names in `approved` are fine. Line breaks are kept."""
    approved_l = {a.lower() for a in approved}
    offending = [n for n in catalog_names if n.lower() not in approved_l and mentioned(answer, n)]
    if not offending:
        return answer, []
    lines = []
    for line in answer.split("\n"):
        pieces = re.split(r"(?<=[.!?])\s+", line)
        kept = [p for p in pieces if p.strip() and not any(mentioned(p, n) for n in offending)]
        if kept:
            lines.append(" ".join(kept))
        elif not line.strip():
            lines.append("")                                  # an empty line between paragraphs stays
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text, offending


def fallback_after_cut(offending: list[str], approved: list[str], labels: list[str] | None = None) -> str:
    return blocked_protocol("recommend", offending[0], labels, approved)


# ---------------------------------------------------------------------------------------------------------------- internal wording the user should never read
_WORDING = [
    (re.compile(r"not on (the |your |my )?approved( exercise)? list( for your condition)?", re.I), "not something I can recommend for your condition"),
    (re.compile(r"\b(the |your |my )?approved( exercise| exercises)? list\b", re.I), "my list of recommended exercises"),
    (re.compile(r"\bapproved exercises\b", re.I), "exercises I can recommend"),
    (re.compile(r"(the )?\bretrieved information\b", re.I), "my reviewed sources"),
]


def clean_wording(answer: str) -> str:
    """The model sometimes says 'the approved exercise list' in its own words even though the prompt forbids it: rewrite it in plain words."""
    for rx, repl in _WORDING:
        answer = rx.sub(repl, answer)
    return answer


# ---------------------------------------------------------------------------------------------------------------- free refusals for a bare link or a bare yes / no
BARE_URL_MESSAGE = "I can't open links. Please tell me where it hurts and for how long, for example: knee pain for 2 weeks."
BARE_REPLY_MESSAGE = ("I'm not sure what you are saying yes or no to. Tap one of the buttons under an answer, or ask me a question - for example: which exercises help my knee?")
SET_TARGET_MESSAGE = ("I can't change your targets from chat. They go up automatically every week when you complete them. To raise one safely, ask me: can I increase my target? "
                      "To change one yourself, open the exercise player, go to that exercise and use Edit.")
_URL = re.compile(r"(https?://|www\.)\S+", re.I)
_BARE_REPLY = re.compile(r"\s*(yes|yeah|yep|yup|nope|nah|no|haan|han|ha|nahi|nahin|nhi|both|dono|all|sab|none|neither|first|second)\W*\s*", re.I)
_SET_TARGET = re.compile(r"\b(set|change|update|make|raise|lower|reduce|increase|decrease|put)\b[^.?!]{0,40}\b(target|goal|reps?|sets?|distance|duration)\b[^.?!]{0,40}\d|"
                         r"\b(target|goal)\b[^.?!]{0,30}\bto\b\s*\d", re.I)
_ALL_TARGETS = re.compile(r"\b(my targets?|targets? (today|now)|today|all|every|each|sab|saare|sabhi)\b", re.I)


def is_bare_url(text: str) -> bool:
    """A message that is only a link (or a link with fewer than 3 letters of other text). The chat cannot open links, and the words inside a link must not be read as a question."""
    t = text or ""
    return bool(_URL.search(t)) and sum(ch.isalpha() for ch in _URL.sub(" ", t)) < 3


def is_bare_reply(text: str) -> bool:
    return bool(_BARE_REPLY.fullmatch(text or ""))


_SET_TARGET_HI = re.compile(r"(target|goal|लक्ष्य|टारगेट)[^.?!]{0,30}(kar\s?do|kar\s?de|badal|badha|kam\s?kar|कर\s?दो|बदल|कम|बढ़ा)", re.I)


def asks_to_set_target(text: str) -> bool:
    """'set my walking target to 100 reps': a request to CHANGE a target to a number. 'can I increase my target?' (no number) is not matched."""
    return bool(_SET_TARGET.search(text or "") or _SET_TARGET_HI.search(text or ""))


def wants_all_targets(text: str) -> bool:
    """'what is my target today?', 'my targets': every exercise, not only the one mentioned in the last exchange."""
    return bool(_ALL_TARGETS.search(text or ""))


# ---------------------------------------------------------------------------------------------------------------- the four bugs found by reading what users saw
_FILLER = frozenset("please pls can you could kindly to in into on my the a an plan program routine list dashboard exercise exercises again back me i want would like also".split())
_ADD_TOKENS = frozenset("add put include insert resume restore enable activate turn jodo jod daalo shamil".split())
_ADD_TOKENS_HI = frozenset("daal dal do de na kar kr karo lagao chalo hi bhi ko mein me program plan routine".split())
_ASKS_ASSISTANT = re.compile(r"\b(did you (suggest|say|tell|recommend|mention|advise)|you (suggested|recommended|told|said|mentioned|advised))\b", re.I)


def fast_add_name(text: str) -> str | None:
    """For a short message that is ONLY an add command plus an exercise name ("add walking", "add glute bridge to my plan", "turn my wall calf stretch back on"): the words that
    name the exercise. Such a message carries no symptom, so it needs no embedding check, urgent AI check or router: it is a free lookup. None for anything longer or mixed."""
    t = (text or "").strip()
    if not t or len(t) > 300 or not has_add_verb(t) or _REMOVE.search(t) or _REMOVE_HI.search(t):
        return None
    t = _SPLIT_EX.sub(" and ", t)                                         # keep list separators (",", "&", "+", "aur") as words so the parts can be split again
    rest = [w for w in re.findall(r"[a-z]+", to_latin(t).lower()) if w not in _FILLER and w not in _ADD_TOKENS and w not in _ADD_TOKENS_HI]
    parts = split_exercise_parts(" ".join(rest))
    # one add command is either a single short name, or a list whose every part is a short name: no limit on how many exercises are listed
    if not rest or (len(parts) <= 1 and len(rest) > 4) or any(len(pt.split()) > 4 for pt in parts):
        return None
    return " ".join(rest)


def asks_what_assistant_said(text: str) -> bool:
    """'which exercises did you suggest earlier?': a question about the CHAT, not about the user's workout log."""
    return bool(_ASKS_ASSISTANT.search(text or ""))


def paused_message(display_name: str) -> str:
    return f"{display_name} is paused in your program, so I have no current progress for it. Say \"add {display_name.lower()}\" to turn it back on."


def question_names(question: str, display_name: str) -> bool:
    """True when the question is about this exercise: its name, or a word that starts like its first word ('run' for Running)."""
    first = display_name.split()[0].lower()
    words = re.findall(r"[a-z]+", (question or "").lower())
    def same(q: str, w: str) -> bool:                       # whole-word or simple inflection only: "run"/"running", "walk"/"walking"; never "show"/"shoulder"
        return q == w or w in (q + "ing", q + "ning", q + "s", q + "es") or q in (w + "s", w + "es")
    return mentioned(question, display_name) or any(len(w) >= 3 and same(w, first) for w in words)


# ---------------------------------------------------------------------------------------------------------------- findings of the safety review: minors, after delivery, surgery, self-harm wording
MINOR_MESSAGE = ("Exercise plans for children and teenagers need a doctor or a qualified trainer who can see the child. Please speak to a doctor first.")
SURGERY_MESSAGE = ("Whether surgery is needed is a decision for your doctor, who can examine you. Please speak to your doctor about it. I can suggest gentle exercises if you ask.")
# DRAFT wording (owner and a doctor should review): kind, says to consult a doctor, never mentions a hospital or an emergency number.
SELF_HARM_MESSAGE = ("I'm really sorry you are going through this. Please consult a doctor or a mental health professional as soon as you can, and talk to someone you trust. "
                     "You don't have to carry this alone.")
_MINOR = re.compile(r"\b(i am|i'm|im|main|mai|meri umar)\s+(\d{1,2})\s*(years?|yrs?|saal|sal|y/?o)\b|\b(my|mera|meri)\s+(son|daughter|child|kid|boy|girl|nephew|niece|beta|beti|bacha|bachi)\b"
                    r"[^.?!]{0,30}\b(\d{1,2})\s*(years?|yrs?|saal|sal)\b|\bi am (a )?(child|kid|teenager|schoolboy|schoolgirl)\b", re.I)
_POSTPARTUM = re.compile(r"\b(postpartum|post[- ]partum|after (the )?(delivery|birth|childbirth)|c-?section|caesarean|cesarean|newborn|breast ?feeding|lactating|gave birth|"
                         r"(just |recently )?(had|have|delivered) (a |my )?(baby|child|twins)|bachcha hua|delivery hui|delivery ke baad|garbh)\b", re.I)
_SURGERY_Q = re.compile(r"\b(surger(y|ies)|surgical|operation|operate on|(knee|hip|joint|shoulder|total) replacement|arthroscop\w*|laminectomy|discectomy|spinal fusion|"
                        r"ligament reconstruction|acl reconstruction|meniscectomy)\b", re.I)


def is_minor(text: str) -> bool:
    """The user says they (or the person the plan is for) are under 18."""
    for m in _MINOR.finditer(text or ""):
        ages = [int(g) for g in m.groups() if g and g.isdigit()]
        if not ages or min(ages) < 18:
            return True
    return False


def is_postpartum(text: str) -> bool:
    return bool(_POSTPARTUM.search(text or ""))


def asks_about_surgery(text: str) -> bool:
    return bool(_SURGERY_Q.search(text or ""))


# ---------------------------------------------------------------------------------------------------------------- fixes from the owner's own manual test (2026-11-02)
OUT_OF_SCOPE_MESSAGE = ("I can only help with your exercises, pain and progress. I can't delete accounts, set reminders or notifications, open links or tell jokes. "
                        "For your account, use the settings in your profile or contact support. You can set an alarm on your phone to remember your exercises.")
APP_FACTS = ("APP FACTS (answer questions about the app from these only): the app suggests exercises from a checked list for the user's saved conditions, adds or removes them from the program "
             "with buttons, undoes the last program change, and shows progress and targets. It does NOT send reminders or notifications, does not delete accounts, does not open links, "
             "does not track calories, heart rate or weight, and does not give medicine advice. Never say or imply that it does. Never name any medicine, drug class, supplement or dose in your answer, not even as an example; say to ask a doctor or pharmacist instead.")

V2_RULES = ("\n\n" + APP_FACTS + "\n\nSAVED CONDITIONS: the message lists the user's saved conditions. Only talk about a pain or condition that is listed there or that the user stated in their own message. "
            "Never assume any pain or condition the user did not mention, and if the user says they do not have something, do not bring it up again."
            "\n\nUNVERIFIED ACTIVITIES: if the user asks whether an activity that is NOT on the approved list (any other activity or sport) is good, helpful or safe "
            "for them, do not say it is helpful or harmful. Say you have no verified guidance on it, tell them to ask their doctor or physiotherapist, and then name the approved exercises."
            "\n\nNUMBERS AND OFFERS: never write a number of minutes, sets, reps, seconds or kilometres unless it appears in the retrieved information or the program data. Never ask a "
            "follow-up question, except one short clarifying question when the message is too vague to tell what the user needs, and never offer anything the app cannot do (reminders, notifications, tracking, plans)."
            "\n\nANSWER THE QUESTION ASKED: when the user asks whether they can or should do an activity, or how to do or learn one, answer that question first and plainly (it is on their approved list, it is blocked for their condition, "
            "or you have no verified knowledge of it and cannot explain or coach it), and only then name approved alternatives that build what it needs. Never present approved exercises as if they were instructions for that other activity, "
            "and never describe how to perform an activity that is not on the approved list."
            "\n\nPAIN: when the user asks about ignoring, pushing through or waiting out pain, or says it is persistent or getting worse, tell them to consult a doctor."
            "\n\nVAGUE MESSAGES: when the message is too vague to tell what is wrong or what the user wants, ask one short question about what and where instead of suggesting exercises.")
_GREETING = re.compile(r"\s*(hi+|hello+|hey+|namaste|good (morning|afternoon|evening|night)|thanks?( you)?|thank you|ok(ay)?|bye|good ?bye)\W*\s*", re.I)
_INTRO = re.compile(r"\s*(my name is|mera naam|i am called|call me|am)\s+[A-Za-z]{2,20}( hai)?\W*\s*", re.I)
_OFFER = re.compile(r"^\s*(would you like|do you want|shall i|should i (help|set|create|remind)|can i help you|want me to|let me know if|if you want,? i can|i can also help)\b", re.I)
_REMINDER = re.compile(r"\b(reminders?|remind you|notifications?|alarms?)\b", re.I)
_CONTROLS = re.compile(r"\b(my |meri |mera )?(sugar|blood sugar|glucose|bp|blood pressure)( level| reading)?\s*(is|hai|was|has been)?\s*(very |too |bahut |zyada |quite )?(high|badh\w*|elevated|"
                       r"uncontrolled|\d{3})\b|\b(i have|i've|mujhe) (high|bahut) (sugar|blood sugar|bp|blood pressure)\b|\b(bp|blood pressure)\b[^.?!]{0,12}\d{2,3}\s*/\s*\d{2,3}", re.I)
_AVOID = re.compile(r"\b(what (should|do) i avoid|what (should|can|must) i not do|things? to avoid|avoid list|precautions?|dos and don'?ts|kya nahi karna|kya na karu|kya avoid|"
                    r"which (exercises|movements) (should i|to) avoid)\b", re.I)
_ROUTINE = re.compile(r"\b(routine|schedule|timetable|weekly plan|week plan|plan for (the|this) week|weekly workout|7[- ]day|seven day|daily plan|hafte ka plan)\b", re.I)
_INCREASE = re.compile(r"\b(increase|raise|boost|badha\w*|level up|upgrade)\b[^.?!]{0,25}\b(target|goal|reps?|sets?|difficulty|load)\b|\b(ready|time) (for|to) (more|progress|level up|increase)\b|"
                       r"\bshould i (do|try) more\b", re.I)


def is_greeting_or_intro(text: str) -> bool:
    """'hello', 'thanks', 'my name is Priya', 'am Ayush': the reply must not bring up old topics, so the model gets no chat history for these."""
    return bool(_GREETING.fullmatch(text or "") or _INTRO.fullmatch(text or ""))


def strip_offers(answer: str) -> str:
    """Remove 'Would you like me to ...?' offers and anything about reminders: the app cannot do them, and a 'yes' reply would lead nowhere."""
    paragraphs = []
    for para in (answer or "").split("\n\n"):
        kept = [x for x in re.split(r"(?<=[.!?])\s+", para.strip()) if x and not _OFFER.search(x) and not _REMINDER.search(x)]
        if kept:
            paragraphs.append(" ".join(kept))
    return "\n\n".join(paragraphs) if paragraphs else answer


def says_high_sugar_or_bp(text: str) -> bool:
    return bool(_CONTROLS.search(text or ""))


def asks_what_to_avoid(text: str) -> bool:
    return bool(_AVOID.search(text or ""))


def asks_routine(text: str) -> bool:
    return bool(_ROUTINE.search(text or ""))


def asks_increase_target(text: str) -> bool:
    return bool(_INCREASE.search(text or ""))


def clean_cautions(row_caution: str, limit: int = 5) -> list[str]:
    """The table row's cautions without the source names in brackets, no duplicates."""
    out = []
    for c in (row_caution or "").split("|"):
        c = re.sub(r"\s*\([^)]*\)\s*\.?\s*$", ".", c.strip()).strip()
        if c and c not in out:
            out.append(c)
    return out[:limit]


def avoid_text(cautions: list[str], approved: list[str]) -> str:
    lines = "\n".join(f"- {c}" for c in cautions) or "- Stop if your pain gets worse."
    return (f"For your saved health conditions, take care with this:\n{lines}\n\nExercises I can suggest for you: {_list(approved)}. "
            "If something hurts or feels wrong, stop and speak to your doctor.")


def weekly_routine(items: list[str], cautions: list[str]) -> str:
    """A simple week built ONLY from the user's own approved exercises and their own current targets (no invented minutes or sets)."""
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    work, k, lines = {0, 1, 3, 4, 5}, 0, []
    for i, day in enumerate(days):
        if i in work:
            lines.append(f"{day}: {items[k % len(items)]}")
            k += 1
        else:
            lines.append(f"{day}: Rest")
    tips = "\n".join(f"- {c}" for c in cautions[:3])
    return ("A simple week using your own exercises and your own current targets:\n" + "\n".join(lines)
            + ("\n\nTake care:\n" + tips if tips else "") + "\n\nStop if your pain gets worse. Your targets go up automatically every week when you complete them.")


def suggest_exercise(text: str, catalog: dict[str, dict]) -> str | None:
    """'angle circles' -> 'Ankle Circles' (a typo): the closest exercise name, or None."""
    import difflib
    names = {info["name"].lower(): info["name"] for info in catalog.values()}
    m = difflib.get_close_matches((text or "").strip().lower(), list(names), n=1, cutoff=0.75)
    return names[m[0]] if m else None


# ---------------------------------------------------------------------------------------------------------------- evasion-proof text for the FREE word rules
# The free rules (urgent words, self-harm, instruction phrases) must not be dodged by "c h e s t pain", zero-width characters, look-alike Cyrillic letters, "su1cide" or
# "k.i.l.l myself". The rules are checked on the original text AND on this normalised text. The AI urgent check remains the backup for anything still missed.
import unicodedata  # noqa: E402

_ZERO = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u200e\u200f\u202a\u202b\u202c\u202d\u202e"), None)
_HOMOGLYPH = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "һ": "h", "к": "k", "м": "m", "т": "t", "в": "b",
                            "н": "h", "ο": "o", "α": "a", "ε": "e", "ι": "i", "ν": "v", "ρ": "p", "τ": "t", "υ": "u", "κ": "k"})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_SPACED = re.compile(r"\b([a-z])(?:[\s.\-_*]+[a-z]\b){2,}")
_COMPACT_URGENT = ("chestpain", "heartattack", "cantbreathe", "cannotbreathe", "chesthurt")
_COMPACT_SELF = ("killmyself", "endmylife", "suicide", "wanttodie", "endithall", "hurtmyself")


def normalize_for_rules(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_ZERO).translate(_HOMOGLYPH).lower()
    t = re.sub(r"[a-z0-9@$]+", lambda m: m.group(0).translate(_LEET) if re.search(r"[a-z]", m.group(0)) and re.search(r"[0-9@$]", m.group(0)) else m.group(0), t)
    t = _SPACED.sub(lambda m: re.sub(r"[^a-z]", "", m.group(0)), t)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", t)).strip()


def compact_hit(text: str) -> str | None:
    """'chestpain' / 'killmyself' typed without spaces: 'urgent', 'self_harm' or None (short messages only, so a long text cannot match by accident)."""
    t = unicodedata.normalize("NFKC", text or "").translate(_ZERO).translate(_HOMOGLYPH).lower()
    if len(t) > 80:
        return None
    c = re.sub(r"[^a-z]", "", re.sub(r"[a-z0-9@$]+", lambda m: m.group(0).translate(_LEET), t))
    if any(w in c for w in _COMPACT_SELF) and not _NEG_SELF.search(t):
        return "self_harm"
    if any(w in c for w in _COMPACT_URGENT):
        return "urgent"
    return None


def fallback_generic(approved: list[str]) -> str:
    """The model's answer was mostly about an unapproved exercise the user never named: say what CAN be suggested, without an irrelevant 'I can't recommend X'."""
    tail = f" Here is what I can suggest for you: {_list(approved)}." if approved else ""
    return f"I can only suggest exercises that are checked for your conditions.{tail} For any other activity, please ask your doctor or physiotherapist first."


# ---------------------------------------------------------------------------------------------------------------- fixed messages for the risk classes (NEW_RISK_CLASSIFIER)
MEDICINE_MESSAGE = ("Which medicine, supplement, dose or treatment is right for you is a question for your doctor or pharmacist. I can't advise on it. Please speak to your doctor.")
EATING_MESSAGE = ("Starving yourself, skipping meals for days or making yourself vomit can seriously harm your body. Please speak to a doctor about this. "
                  "I can't suggest exercise for weight loss like that.")
DIAGNOSIS_MESSAGE = "I can't tell what is causing this. Please see a doctor who can examine you and tell you what it is."


# ---------------------------------------------------------------------------------------------------------------- things the app cannot do, in any language (free, fixed messages)
_REMIND = re.compile(r"\b(remind(er)?s?|alarms?|notifications?|notify)\b|yaad\s?dila|(रिमाइंडर|नोटिफिकेशन|याद\s?दिला|अलार्म)", re.I)
_DELETE_ACCOUNT = re.compile(r"(\b(delete|remove|close|band|hata\w*)\b[^.?!]{0,25}\b(account|profile|data)\b|\b(account|profile|data)\b[^.?!]{0,20}\b(delete|hata\w*|band|remove)\b|(खाता|अकाउंट)[^.?!]{0,20}(डिलीट|हटा|बंद))", re.I)
REMINDER_MESSAGE = "I can't set reminders or send notifications. You can set an alarm on your phone to remember your exercises."
ACCOUNT_MESSAGE = "I can't delete your account from chat. Please use the settings in your profile, or contact support."


def asks_reminder(text: str) -> bool:
    return bool(_REMIND.search(text or ""))


def asks_delete_account(text: str) -> bool:
    return bool(_DELETE_ACCOUNT.search(text or ""))


_YES_NO = re.compile(r"^\s*(is|are|can|could|may|should|does|do|will|kya)\b", re.I)
_NOT_YES_NO = re.compile(r"\b(exercises?|routine|plan|suggest|recommend|what|which|how)\b", re.I)


def is_yes_no_question(text: str) -> bool:
    """'is yoga good for me?', 'can I squat?', 'aur cycling?': a yes/no question about one activity, not a request for an exercise list."""
    t = text or ""
    return bool(_YES_NO.search(t)) and not _NOT_YES_NO.search(t)


# ---------------------------------------------------------------------------------------------------------------- several exercises in one add command, and "both"
_SPLIT_EX = re.compile(r"\s*(?:,|&|\+|\band\b|\baur\b|\bor\b|और|तथा)\s*", re.I)
_CHOICE_FOLLOWUP = re.compile(r"\s*(both|dono|dono hi|dono ko|all|all of them|both of them|sab|sabhi|teeno|दोनों|सभी|सब)\W*\s*", re.I)
CHOICE_PREFIXES = ("Which one do you mean: ", "Which exercise would you like to add?")


def split_exercise_parts(text: str) -> list[str]:
    """'running and ankle circles' -> ['running', 'ankle circles'] (also ',', '&', '+', 'aur', 'और')."""
    return [p.strip() for p in _SPLIT_EX.split(text or "") if p.strip()]


def is_choice_followup(text: str) -> bool:
    """'both', 'dono', 'all': the answer to our own 'Which one do you mean: A, B?' question."""
    return bool(_CHOICE_FOLLOWUP.fullmatch(text or ""))


def not_approved_note(display_names: list[str], labels: list[str] | None = None) -> str:
    return blocked_protocol("add", _list(display_names), labels, [])
