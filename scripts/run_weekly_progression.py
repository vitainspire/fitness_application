"""Module 15 weekly progression job — interim scheduler entry point.

Implementation_RLM_plan.md Phase 2, option 1: no Celery worker exists in this
codebase yet, so this is invoked by OS-level scheduling (cron / Windows Task
Scheduler) instead of a periodic Celery task. The job logic lives in
app.progression.service, decoupled from how it's invoked — moving to a real
queue later is a scheduling change only.

Per-user cadence (explicit product decision): the scheduler now fires DAILY,
and this script only processes a user whose signup weekday matches today —
so each user is still evaluated exactly once every 7 days, just staggered
across the week by when they joined, instead of every account being
recalculated on the same fixed day. The math and pacing inside
run_weekly_progression are unaffected either way; the trailing-window
functions it calls already look back a rolling 7 days from "today", not a
calendar-week boundary — this only changes WHICH day each user's "today" is.

Usage: python scripts/run_weekly_progression.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
dev_env = REPO_ROOT / ".env.dev"
if dev_env.exists():
    load_dotenv(dev_env)

from sqlalchemy import select  # noqa: E402

from app.platform.config import load_config  # noqa: E402
from app.platform.db import init_engine, session_scope  # noqa: E402

# Imported for side effect only: registers every table on Base.metadata so the
# FK from user_program/progression_history -> users resolves. This script only
# touches profile/catalog/progression models directly, but SQLAlchemy needs the
# whole mapped graph present, not just the tables this file happens to import.
import app.catalog.models  # noqa: E402,F401
from app.auth.models import User  # noqa: E402

from app.profile.models import UserProgram  # noqa: E402
from app.profile.service import today_ist  # noqa: E402
from app.progression.service import run_daily_progression_batch  # noqa: E402

_log = logging.getLogger(__name__)


def main() -> None:
    cfg = load_config()
    if not cfg.rlm_weekly_progression_enabled:
        # A no-op exit, not an error: lets the cron/Task Scheduler entry stay
        # registered and simply do nothing while this is switched off
        # (RLM_WEEKLY_PROGRESSION_ENABLED=false), rather than needing the
        # scheduler entry itself removed and re-added later (Phase 7).
        print("RLM_WEEKLY_PROGRESSION_ENABLED=false — skipping this run.")
        return
    init_engine(cfg.database_url)
    res = run_daily_progression_batch()
    print(f"Progression run complete: {res['total_users']} user(s) due today, "
          f"{res['total_adjustments']} entry adjustment(s), {res['failed_users']} failed.")


if __name__ == "__main__":
    # Found live (code audit): this setdefault silently reintroduced the
    # exact "silent development default" behavior config.py's own fail-fast
    # requirement (FLASK_ENV is now _require()'d, not _optional()'d) was
    # meant to eliminate everywhere — for any invocation of this script that
    # doesn't go through run_weekly_progression.ps1 (which sets FLASK_ENV
    # explicitly, making this a no-op there), e.g. a manual `python
    # scripts/run_weekly_progression.py`, or a differently-configured
    # scheduler entry on a future host. Removed: if FLASK_ENV isn't set,
    # load_config() (called inside main()) now correctly refuses to run
    # instead of silently assuming development.
    main()
