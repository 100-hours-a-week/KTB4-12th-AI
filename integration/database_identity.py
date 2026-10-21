"""All application DSNs must name the same prepared PG service/database."""
import os
import re
from urllib.parse import urlsplit, parse_qs
import psycopg

DEFAULT_HOST = "postgres"


VERSION = "single-server/1"
DEPLOYMENT_DATABASES = frozenset({"ai_db_develop", "ai_db_staging", "ai_db_production"})


def validate_database_name(name):
    """Allow explicit environment DBs; retain legacy names only for synthetic tests."""
    if name in DEPLOYMENT_DATABASES:
        return name
    if os.environ.get("INTEGRATION_SYNTHETIC_ONLY") == "1" and re.fullmatch(
        r"emmet4930_[a-z0-9_]+", name
    ):
        return name
    raise ValueError("AI_DATABASE_NAME must identify an approved environment database")


def configured_host():
    """The one PostgreSQL host every DSN must name: `postgres` locally, the RDS endpoint on ECS."""
    host = os.environ.get("AI_DATABASE_HOST", "").strip() or DEFAULT_HOST
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
        raise ValueError("AI_DATABASE_HOST must be a bare hostname")
    return host


def validate_dsn(value, expected_database, expected_role, *, host=None, port=5432):
    host = host or configured_host()
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
        if (actual, declared, version) != (expected_database, expected_database, VERSION):
            raise ValueError("Wrong application database identity or missing migration")
