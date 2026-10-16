"""Flask application factory. One deployable; service modules inside (STRATEGY §2-3)."""
from __future__ import annotations

import logging
import os
import uuid

from flask import Flask, g, jsonify, request
from flask_cors import CORS

from app.platform import redis_clients as rc
from app.platform.config import load_config
from app.platform.db import init_engine
from app.platform.converters import UuidStrConverter
from app.platform.errors import register_error_handlers


def create_app() -> Flask:
    cfg = load_config()

    app = Flask(__name__)
    # Must be registered before any blueprint adds its rules (see converters.py).
    app.url_map.converters["uid"] = UuidStrConverter
    app.config["APP_CONFIG"] = cfg
    app.config["SECRET_KEY"] = cfg.secret_key
    # Found live (pre-launch production readiness check): nothing capped
    # request body size at all — every route is JSON-only with small,
    # bounded legitimate payloads (the largest realistic one is a batch
    # activity-log POST), so there's no legitimate reason for a multi-MB
    # request. Without this, an unauthenticated caller could send an
    # arbitrarily large body to any endpoint, forcing Flask to buffer all of
    # it before a route handler (or even JSON parsing) ever runs — a cheap
    # resource-exhaustion angle that matters a lot more once this is actually
    # internet-reachable than it did in local dev. 1MB is generous headroom
    # over any real request this app sends.
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

    # The frontend (Vercel) is a different origin than this API (Railway), so
    # browser fetches are cross-origin by default and get blocked without
    # this. No cookies are used for auth (Bearer tokens only), so credentials
    # support is deliberately left off — a wildcard-with-credentials
    # combination is a real CORS misconfiguration this avoids entirely.
    if cfg.cors_origins:
        CORS(app, origins=cfg.cors_origins, supports_credentials=False)

    logging.basicConfig(
        level=logging.INFO if cfg.is_production else logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    init_engine(cfg.database_url, echo=False)
    # Fallback is development-only; production must have a real Redis (STRATEGY §6).
    rc.init_redis(
        cfg.redis_durable_url, cfg.redis_cache_url,
        allow_fallback=not cfg.is_production,
    )

    register_error_handlers(app)

    @app.before_request
    def _assign_request_id():
        """10.1 — one id per request, so an issue row and its logs can be
        tied back to the same request even across processes."""
        g.request_id = request.headers.get("X-Request-Id") or f"req-{uuid.uuid4().hex[:12]}"

    @app.after_request
    def _echo_request_id(response):
        response.headers["X-Request-Id"] = getattr(g, "request_id", "")
        return response

    # Capacity isolation (see Config.service_role's own comment for the full
    # reasoning): "all" — the default, unchanged from before — registers
    # everything on one deployable, exactly as this app has always run.
    # Only when a SECOND deployable is deliberately stood up with
    # SERVICE_ROLE=web or SERVICE_ROLE=chat does either half stop carrying
    # routes it isn't meant to serve. Until that second deployable exists,
    # this is a no-op.
    if cfg.service_role in ("all", "web"):
        from app.auth.routes import account_bp, bp as auth_bp
        from app.profile.routes import bp as core_bp
        from app.relationships.routes import bp as relationships_bp
        app.register_blueprint(auth_bp)
        app.register_blueprint(account_bp)
        app.register_blueprint(core_bp)
        app.register_blueprint(relationships_bp)
    if cfg.service_role in ("all", "chat"):
        from app.rag.routes import bp as rehab_bp
        from app.voice.routes import bp as voice_bp
        app.register_blueprint(rehab_bp)
        app.register_blueprint(voice_bp)

    if not cfg.is_production:
        # Never mounted in production - it exists to drive the API by hand.
        from app.devconsole.routes import bp as dev_bp
        app.register_blueprint(dev_bp)

    @app.get("/")
    def index():
        """Dev-friendly root. A JSON API whose root 404s is hostile to anyone
        poking at it in a browser; this lists what actually exists.

        Found live (code audit): the full route listing used to run
        unconditionally, in every environment — handing any anonymous caller
        a complete map of the API surface (every path and method) in
        production, with zero effort. The listing itself is now
        non-production only; production gets a minimal, honest response.
        """
        if cfg.is_production:
            return jsonify({"service": "fitness-app-api", "env": cfg.env})
        routes = []
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
            if rule.endpoint == "static":
                continue
            routes.append({
                "methods": sorted(rule.methods - {"HEAD", "OPTIONS"}),
                "path": rule.rule,
            })
        return jsonify({
            "service": "fitness-app-api",
            "env": cfg.env,
            "note": "JSON API only - there is no web client (Module 11).",
            "routes": routes,
        })

    @app.get("/healthz")
    def healthz():
        # Found live (code audit): this used to be a static response — it
        # never touched the database, so it reported "ok" even against a
        # completely EMPTY database (no migration run, no reference data
        # loaded). Railway's own deploy health check hits this exact path
        # (railway.json), so a deploy where the release step was skipped
        # showed a green "healthy" checkmark while every real endpoint 500s
        # or returns nonsense — the failure was invisible until a real user
        # hit it. A cheap read against the reviewed reference data both
        # confirms the database is actually reachable post-boot (not just at
        # the one-time probe in init_engine) and that it was actually seeded.
        try:
            from sqlalchemy import select

            from app.catalog.models import ActivityTaxonomy
            from app.platform.db import session_scope
            with session_scope() as session:
                seeded = session.scalar(select(ActivityTaxonomy.activity_type).limit(1))
            if seeded is None:
                return jsonify({
                    "status": "error", "env": cfg.env,
                    "detail": "Database reachable but has no reference data — "
                              "run `python -m app.catalog.loader`.",
                }), 503
        except Exception as exc:
            return jsonify({"status": "error", "env": cfg.env, "detail": str(exc)}), 503
        return jsonify({"status": "ok", "env": cfg.env})

    @app.get("/v1/app/config")
    def app_config():
        """11.10 - unauthenticated, checked on launch before login."""
        return jsonify({
            "minimum_supported_version": os.environ.get("MIN_APP_VERSION", "1.0.0"),
            "soft_update_version": os.environ.get("SOFT_UPDATE_VERSION", "1.0.0"),
        })

    return app
