"""The reps / sets / targets route (NEW_ROUTER=true): the answer is built from the user's own program rows (no AI call, no notes search)."""
from decimal import Decimal

UNIT = {"count": "reps", "sec": "seconds", "km": "km", "min": "minutes"}


def _n(x):
    x = Decimal(x)
    return str(int(x)) if x == x.to_integral() else f"{x:.2f}".rstrip("0").rstrip(".")


def one(name, sets, amount, unit, set_targets, auto):
    u = UNIT.get(unit, unit)
    if set_targets and len(set_targets) > 1:
        per = ", ".join(_n(s) for s in set_targets)
        s = f"{name}: {len(set_targets)} sets of {per} {u}"
    elif sets and sets > 1:
        s = f"{name}: {sets} sets of {_n(amount)} {u}"
    else:
        s = f"{name}: {_n(amount)} {u}"
    return s


def answer(rows, asked_name=None):
    """rows: (display_name, status, target_sets, target_amount, unit, set_targets, auto_progress) of the user's program. asked_name: an exercise the user named, or None."""
    active = [r for r in rows if r[1] == "active"]
    if not active:
        return "You don't have any active exercises in your program yet, so there are no targets to show. Add an exercise first and I can tell you its target."
    pick = [r for r in active if asked_name and asked_name.lower() in r[0].lower()]
    if asked_name and not pick:
        paused = [r for r in rows if r[1] != "active" and asked_name.lower() in r[0].lower()]
        return (f"{paused[0][0]} is paused in your program right now, so it has no active target." if paused else f"I can't find {asked_name} in your program.")
    use = pick or active
    lines = "\n".join("- " + one(r[0], r[2], r[3], r[4], r[5], r[6]) for r in use)
    auto = any(r[6] for r in use)
    tail = "\n\nThese targets are based on your level and how you have done so far" + (", and they go up automatically every week when you complete them." if auto else ".")
    return ("Your current target" + ("s" if len(use) > 1 else "") + ":\n" + lines + tail)
