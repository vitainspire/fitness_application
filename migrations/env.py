import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy import pool

from alembic import context

# So `app.*` imports resolve regardless of the working directory this is run
# from — same pattern as scripts/run_weekly_progression.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Importing every model module (not just Base) is what actually registers
# each table on Base.metadata — SQLAlchemy only knows about a model once its
# module has been imported somewhere. Missing one here would silently drop
# that table from every autogenerate diff, so this list must be kept in sync
# with app/*/models.py.
from app.platform.db import Base  # noqa: E402
import app.auth.models  # noqa: E402,F401
import app.catalog.models  # noqa: E402,F401
import app.platform.models  # noqa: E402,F401
import app.profile.models  # noqa: E402,F401
import app.progression.models  # noqa: E402,F401
import app.rag.models  # noqa: E402,F401
import app.relationships.models  # noqa: E402,F401
from app.platform.config import load_config  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Drive the connection URL from the app's own config (same DATABASE_URL every
# other entry point uses — env-var driven, already normalizes postgres:// /
# postgresql:// to the psycopg driver form) rather than a separate value
# hardcoded in alembic.ini, so migrations always target whatever database the
# app itself would connect to.
#
# Found live (real deploy attempt): alembic.Config's set_main_option/
# get_main_option round-trips through Python's ConfigParser, which treats a
# literal `%` as the start of `%(name)s`-style interpolation syntax — a
# database password containing a URL-escaped character (e.g. `%40` for `@`,
# a completely normal thing for a password to contain) raised an uncaught
# ValueError ("invalid interpolation syntax") the instant it was stored, long
# before any real database connection was attempted. Kept as a plain Python
# variable instead, never handed to Config/ConfigParser at all, so no
# character in the URL needs escaping for alembic's sake.
db_url = load_config().database_url

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# rag_chunks lives in this same database today but is created and owned by
# scripts/dev_tools/setup_rag_schema.py, not by any SQLAlchemy model — it was
# meant to live in a separate Supabase project (STRATEGY §5: "the vector store
# stays out of the primary datastore's blast radius"), but currently shares
# this DATABASE_URL. Because it's absent from Base.metadata, autogenerate sees
# it as "a table that shouldn't be here" and will happily emit
# op.drop_table('rag_chunks') the next time anyone runs --autogenerate — which
# would delete the entire embedded RAG corpus. Excluding it here is the
# backstop until the two databases are actually split apart.
_IGNORED_TABLES = {"rag_chunks"}


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name in _IGNORED_TABLES:
        return False
    return True

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = create_engine(
        db_url,
        poolclass=pool.NullPool,
        # Matches app/platform/db.py's init_engine() exactly — Supabase's
        # pooler runs in transaction mode, which doesn't hold server-side
        # prepared statements across pooled connections, and psycopg's
        # default prepare-after-N-uses behavior then collides with a
        # statement name a previous connection already prepared. This engine
        # is built separately from init_engine() (Alembic manages its own
        # connection lifecycle), so it needs the same fix applied here too —
        # without it, running a migration against Supabase's pooled port
        # can fail with a "prepared statement already exists" error.
        connect_args={"prepare_threshold": None} if "psycopg" in db_url else {},
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
