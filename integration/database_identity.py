"""All application DSNs must name the same prepared PG service/database."""
from urllib.parse import urlsplit, parse_qs
import psycopg


def validate_dsn(value, expected_database, expected_role, *, host="postgres", port=5432):
    parsed = urlsplit(value.replace("postgresql+psycopg://", "postgresql://"))
    query = parse_qs(parsed.query)
    if (parsed.scheme != "postgresql" or parsed.hostname != host or (parsed.port or 5432) != port
            or parsed.path != "/" + expected_database or parsed.username != expected_role
            or any(k in query for k in ("host", "hostaddr", "port", "dbname", "user", "service"))):
        raise ValueError("Application DSNs must use the configured single database and service role")
    return value


def audit(value, expected_database):
    with psycopg.connect(value.replace("postgresql+psycopg://", "postgresql://"), connect_timeout=3) as db:
        actual, declared, version = db.execute("""SELECT current_database(),database_name,version
            FROM ai_runtime.database_identity WHERE singleton""").fetchone()
        if (actual, declared, version) != (expected_database, expected_database, "single-server/1"):
            raise ValueError("Wrong application database identity or missing migration")
