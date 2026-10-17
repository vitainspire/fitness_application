"""URL converters shared by every blueprint."""
from werkzeug.routing import BaseConverter


class UuidStrConverter(BaseConverter):
    """`<uid:name>` — matches only a canonical UUID and hands the route the
    plain string (werkzeug's built-in `uuid` converter hands over a uuid.UUID
    object, which would change what every service function receives).

    Found live (E2E audit): an id column is a Postgres UUID, so a path id like
    "malformed-id" went straight into session.get() and raised a database
    DataError — a 500 for what is just a wrong URL. A non-matching path simply
    doesn't route, which the app's existing 404 handler already renders as the
    standard JSON {"error":{"code":"not_found"}} envelope. A real UUID that
    belongs to nobody still reaches the service and gets its own specific
    NotFound message exactly as before.
    """
    regex = (r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
             r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
