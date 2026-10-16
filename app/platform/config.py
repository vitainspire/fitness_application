"""Configuration with fail-fast validation.

STRATEGY_AND_OPS §11: one canonical variable per concern, validated at boot.
Production refuses to start if anything required is missing — silent degradation
is worse than a failed deploy.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta


class ConfigError(RuntimeError):
    """Raised at boot when required configuration is absent or invalid."""


# Found live (code audit): FLASK_ENV was read with a plain default and compared
# by exact string equality with nothing validating it against this set. Unset,
# misspelled, or wrong-case ("Production", "PROD", "staging", a trailing
# space) all silently fell through to the dev branch below — hardcoded dev
# secrets, hardcoded dev JWT key, the internal devconsole blueprint mounted
# unauthenticated (see app/factory.py), debug-level logging. This one variable
# controls the entire production security posture, so an unrecognized value
# must refuse to boot, not silently degrade.
_VALID_ENVS = {"development", "test", "production"}


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(
            f"{name} is required and not set. "
            "Production must not start with incomplete configuration (STRATEGY §11)."
        )
    return val


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    env: str
    debug: bool

    secret_key: str
    database_url: str

    # Two Redis instances, not four (STRATEGY §6). Exactly these two names are
    # read; nothing else in the codebase may invent another Redis variable.
    redis_durable_url: str
    redis_cache_url: str

    # --- Module 5: rehab & pain RAG ---------------------------------------
    # A separate Postgres (Supabase + pgvector) from database_url above, by
    # design — the vector store stays out of the primary datastore's blast
    # radius (STRATEGY §5). Not yet required in production: that's a real
    # deploy decision nobody has made, so an unset value here degrades to
    # "Module 5 answers with a config error" rather than refusing to boot.
    rag_database_url: str
    openrouter_api_key: str

    # --- RLM (Implementation_RLM_plan.md Phase 7) -------------------------
    # Independent flags so the silent backend curve (Phase 2) can ship without
    # waiting on the LLM-facing chat analysis (Phase 3/4/5), and either half
    # can be switched off on its own if something needs investigating —
    # without touching code, per the plan's own rollout guidance.
    rlm_weekly_progression_enabled: bool
    rlm_chat_analysis_enabled: bool

    # --- Module 1: tokens -------------------------------------------------
    jwt_current_kid: str
    jwt_keys: dict[str, str]  # kid -> secret. Two active keys allow rotation (1.3)
    access_token_ttl: timedelta
    refresh_token_ttl: timedelta          # sliding window (1.4)
    refresh_absolute_cap: timedelta       # 180d from original login (1.4)
    refresh_grace: timedelta              # ~30s, device-bound (1.4)

    # --- Module 1: password / lockout ------------------------------------
    password_min_length: int
    breach_check_enabled: bool
    login_lockout_threshold: int
    login_lockout_base_seconds: int

    # --- Module 2: rate limits (three independent limits, 2.3) -----------
    rl_user_per_minute: int
    rl_device_per_hour: int
    rl_ip_per_hour: int

    # --- Product constants ------------------------------------------------
    timezone: str = "Asia/Kolkata"   # single market (3.3)

    # --- Service role (capacity isolation) ---------------------------------
    # The chatbot (app/rag) makes several sequential calls to an external LLM
    # provider, each individually slow (up to ~90s) and occasionally much
    # slower — everything else in this app (auth, logging, dashboard) only
    # ever talks to Postgres/Redis and finishes in well under a second. On a
    # single deployable with a small, fixed number of worker threads, a
    # handful of concurrent chat requests can occupy every thread at once,
    # so an unrelated user trying to just log in or check their dashboard
    # gets stuck waiting behind someone else's slow AI reply — in the worst
    # case, one request slow enough to hit gunicorn's own timeout takes the
    # whole process down with it, including every other in-flight request.
    #
    # "all" (default) is the current single-service deployment — nothing
    # changes unless this is set. Deploying a SECOND instance of this exact
    # same codebase with SERVICE_ROLE=chat (serving only app/rag) alongside
    # the first with SERVICE_ROLE=web (serving everything else) gives the
    # chatbot its own separate pool of worker threads, so no amount of chat
    # traffic can ever block login/dashboard/logging again — see
    # app/factory.py for where this actually gates blueprint registration.
    service_role: str = "all"

    # --- CORS ---------------------------------------------------------------
    # The frontend is a separate deployable (Vercel) from this API (Railway),
    # so browser requests are cross-origin by default and get silently blocked
    # without this. Empty in production until a real frontend origin is set —
    # fails closed (no cross-origin access) rather than defaulting to "allow
    # everything", same reasoning as the rest of this file.
    cors_origins: list[str] = field(default_factory=list)

    # --- Devconsole (found live, code audit) -------------------------------
    # factory.py's `if not cfg.is_production` gate is necessary but not
    # sufficient: "development" and "test" are both legitimate non-prod
    # FLASK_ENV values, so a box deliberately or accidentally started with
    # either, but still network-reachable, would leave devconsole wide open
    # with no login of its own. Blank password = devconsole refuses every
    # request outright (fail closed) rather than silently allowing access —
    # see app/devconsole/routes.py's before_request hook.
    devconsole_user: str = "admin"
    devconsole_password: str = ""

    # --- Email / Mailer & Frontend URL ------------------------------------
    frontend_url: str = "http://localhost:5173"
    mail_from: str = "ayush.kumar@vitainspire.com"
    mail_sender_name: str = "ViTAinspire"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False

    @property
    def is_production(self) -> bool:
        return self.env == "production"


def load_config() -> Config:
    # No default (found live, code audit): a silent "development" fallback
    # meant forgetting to set FLASK_ENV anywhere — a fresh deploy, a
    # container image missing the var, a copy-pasted env file with the line
    # dropped — silently ran with dev secrets, the dev JWT key, and
    # devconsole exposed, with no signal anything was wrong. Every real
    # invocation path already sets this explicitly (run.ps1 sources
    # .env.dev, tests/conftest.py sets it directly, the scheduled-job
    # wrapper sets it directly) — requiring it turns "forgot to set this
    # somewhere new" into an immediate boot failure everywhere, not a
    # silent security downgrade on whichever context happens to be real
    # production.
    env = _require("FLASK_ENV")
    if env not in _VALID_ENVS:
        raise ConfigError(
            f"FLASK_ENV={env!r} is not a recognized environment (expected one "
            f"of {sorted(_VALID_ENVS)}). Refusing to boot with an ambiguous "
            "environment, since it silently controls the entire production "
            "security posture (secrets, JWT signing key, devconsole exposure)."
        )
    is_prod = env == "production"

    if is_prod:
        secret_key = _require("SECRET_KEY")
        if len(secret_key) < 32:
            raise ConfigError("SECRET_KEY must be at least 32 characters in production")
        database_url = _require("DATABASE_URL")
        if database_url.startswith("sqlite"):
            raise ConfigError("SQLite is not permitted in production")
        redis_durable = _require("REDIS_DURABLE_URL")
        redis_cache = _require("REDIS_CACHE_URL")
        jwt_current = _require("JWT_CURRENT_KID")
        jwt_secret = _require("JWT_KEY_CURRENT")
        if len(jwt_secret) < 32:
            # Found live (code audit): SECRET_KEY gets this exact floor two
            # lines up, but this key — which signs every access/refresh
            # token in the app — had no minimum at all. A short HMAC secret
            # is brute-forceable offline from any single issued token, which
            # is a full authentication bypass, not just a weak-secrets nit.
            raise ConfigError("JWT_KEY_CURRENT must be at least 32 characters in production")
        # Not yet _require()'d — see the field comment above.
        rag_database_url = _optional("RAG_DATABASE_URL", "")
        openrouter_api_key = _optional("OPENROUTER_API_KEY", "")
    else:
        secret_key = _optional("SECRET_KEY", "dev-only-secret-do-not-use-in-production")
        database_url = _optional(
            "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/fitness"
        )
        redis_durable = _optional("REDIS_DURABLE_URL", "redis://localhost:6379/0")
        redis_cache = _optional("REDIS_CACHE_URL", "redis://localhost:6379/1")
        jwt_current = _optional("JWT_CURRENT_KID", "dev-1")
        jwt_secret = _optional("JWT_KEY_CURRENT", "dev-jwt-signing-key-not-for-production")
        rag_database_url = _optional("RAG_DATABASE_URL", "")
        openrouter_api_key = _optional("OPENROUTER_API_KEY", "")

    # Found live (code audit): nothing enforced this file's own architectural
    # rule (STRATEGY §6) that the durable store and the evictable cache must
    # never share an instance — a copy-paste misconfiguration pointing both
    # at the same Redis would let allkeys-lru eviction silently delete a
    # revocation-denylist entry under memory pressure, making a revoked/
    # logged-out token valid again with no code-level signal anything is wrong.
    if redis_durable == redis_cache:
        raise ConfigError(
            "REDIS_DURABLE_URL and REDIS_CACHE_URL must not be the same value. "
            "The durable store (revocation denylist, rate limits) must never "
            "share an instance with the evictable cache (STRATEGY §6)."
        )

    # Normalise the postgres:// form Supabase and Heroku still emit.
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    # rag_database_url goes through plain psycopg (app/rag/vectorstore.py), never
    # SQLAlchemy, so it needs the bare postgresql:// scheme regardless of how it's
    # written in the environment.
    rag_database_url = rag_database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    # Two active keys so a key can be retired without invalidating live sessions (1.3).
    keys = {jwt_current: jwt_secret}
    prev_kid = os.environ.get("JWT_PREVIOUS_KID", "").strip()
    prev_key = os.environ.get("JWT_KEY_PREVIOUS", "").strip()
    # Validate BEFORE the `both present` test, not inside it. A half-configured
    # rotation (one of the pair set, the other missing or typo'd) would
    # otherwise be silently ignored — and silently ignoring the previous key is
    # exactly the failure that invalidates every session still holding a token
    # signed with it. Fail fast instead, like every other setting here.
    if bool(prev_kid) != bool(prev_key):
        raise ConfigError(
            "JWT_PREVIOUS_KID and JWT_KEY_PREVIOUS must be set together "
            "(key rotation needs both halves, or neither)."
        )
    if prev_kid and prev_key:
        # Same floor as JWT_KEY_CURRENT, for the same reason: decode_access_token
        # accepts a token signed with EITHER configured kid, so a weak retired
        # key is just as forgeable as a weak current one — the rotation
        # mechanism would otherwise be a way to quietly reintroduce the exact
        # weakness the current-key check exists to prevent.
        if is_prod and len(prev_key) < 32:
            raise ConfigError("JWT_KEY_PREVIOUS must be at least 32 characters in production")
        if prev_kid == jwt_current:
            # Otherwise this assignment silently overwrites the CURRENT key's
            # secret with the retired one — every freshly issued token would
            # then fail its own signature check.
            raise ConfigError("JWT_PREVIOUS_KID must differ from JWT_CURRENT_KID")
        keys[prev_kid] = prev_key

    service_role = _optional("SERVICE_ROLE", "all").strip().lower()
    if service_role not in {"all", "web", "chat"}:
        raise ConfigError(
            f"SERVICE_ROLE={service_role!r} is not recognized "
            "(expected one of: all, web, chat)."
        )

    return Config(
        env=env,
        debug=not is_prod,
        secret_key=secret_key,
        database_url=database_url,
        redis_durable_url=redis_durable,
        redis_cache_url=redis_cache,
        rag_database_url=rag_database_url,
        openrouter_api_key=openrouter_api_key,
        rlm_weekly_progression_enabled=_optional(
            "RLM_WEEKLY_PROGRESSION_ENABLED", "true").lower() == "true",
        rlm_chat_analysis_enabled=_optional(
            "RLM_CHAT_ANALYSIS_ENABLED", "true").lower() == "true",
        jwt_current_kid=jwt_current,
        jwt_keys=keys,
        access_token_ttl=timedelta(minutes=_int("ACCESS_TOKEN_TTL_MINUTES", 30)),
        # Product decision: a user should only ever see a login screen again
        # by choosing to log out, never because time passed. The access
        # token still expires every 30 min as before — the frontend already
        # refreshes it silently and transparently (main.js's doRefreshToken)
        # — only the REFRESH token's sliding window and absolute cap are
        # extended here, from 30/180 days to effectively "as long as this
        # app is likely to be used for" rather than infinite (an infinite
        # value would mean a stolen refresh token from a lost device is
        # valid forever too; a very long bound still bounds that).
        refresh_token_ttl=timedelta(days=_int("REFRESH_TOKEN_TTL_DAYS", 3650)),
        refresh_absolute_cap=timedelta(days=_int("REFRESH_ABSOLUTE_CAP_DAYS", 3650)),
        refresh_grace=timedelta(seconds=_int("REFRESH_GRACE_SECONDS", 30)),
        password_min_length=_int("PASSWORD_MIN_LENGTH", 10),
        breach_check_enabled=_optional("BREACH_CHECK_ENABLED", "true").lower() == "true",
        login_lockout_threshold=_int("LOGIN_LOCKOUT_THRESHOLD", 5),
        login_lockout_base_seconds=_int("LOGIN_LOCKOUT_BASE_SECONDS", 30),
        rl_user_per_minute=_int("RL_USER_PER_MINUTE", 120),
        rl_device_per_hour=_int("RL_DEVICE_PER_HOUR", 60),
        rl_ip_per_hour=_int("RL_IP_PER_HOUR", 200),
        devconsole_user=_optional("DEVCONSOLE_USER", "admin"),
        devconsole_password=_optional("DEVCONSOLE_PASSWORD", ""),
        cors_origins=_cors_origins(is_prod),
        service_role=service_role,
        frontend_url=_optional("FRONTEND_URL", _optional("APP_BASE_URL", "http://localhost:5173")).rstrip("/"),
        mail_from=_optional("MAIL_FROM", _optional("SMTP_FROM", "ayush.kumar@vitainspire.com")),
        mail_sender_name=_optional("MAIL_SENDER_NAME", "ViTAinspire"),
        smtp_host=_optional("SMTP_HOST", ""),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=_optional("SMTP_USER", ""),
        smtp_password=_optional("SMTP_PASSWORD", ""),
        smtp_use_tls=_optional("SMTP_USE_TLS", "true").lower() == "true",
        smtp_use_ssl=_optional("SMTP_USE_SSL", "false").lower() == "true",
    )


def _cors_origins(is_prod: bool) -> list[str]:
    # Dev gets a working default (the Vite dev server's own origin) so the
    # local frontend just works with zero setup; production has no default —
    # set CORS_ORIGINS explicitly once a real frontend URL exists.
    default = "" if is_prod else "http://localhost:5173,http://127.0.0.1:5173"
    raw = os.environ.get("CORS_ORIGINS", default)
    return [o.strip() for o in raw.split(",") if o.strip()]
