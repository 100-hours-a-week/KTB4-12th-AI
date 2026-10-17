"""One-shot Search image job: complete BE export -> PG pending generation.

The serving Search performs final validation and activation. This command never
writes the file CURRENT pointer, migrates a DB, or touches a cost ledger.
"""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from integration.containers.entrypoint import prepare_encoder, secret
from integration.database_identity import audit, validate_dsn


def validated_fetcher(export_url, categories_url, token):
    import httpx
    origins = []
    for value in (export_url, categories_url):
        u = urlsplit(value)
        if (u.scheme not in ("http", "https") or not u.hostname or u.username is not None
                or u.password is not None or u.query or u.fragment):
            raise ValueError("EXPLICIT_EXPORT_ORIGIN_REQUIRED")
        if (u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1")
                and os.getenv("BACKEND_EXPORT_ALLOW_PRIVATE_HTTP") != "1"):
            raise ValueError("HTTPS_OR_EXPLICIT_PRIVATE_HTTP_REQUIRED")
        origins.append((u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80)))
    if origins[0] != origins[1] or not token:
        raise ValueError("ONE_AUTHORIZED_EXPORT_ORIGIN_REQUIRED")

    def fetch():
        results = []
        with httpx.Client(timeout=httpx.Timeout(60, connect=5), trust_env=False, follow_redirects=False) as client:
            for url in (export_url, categories_url):
                with client.stream("GET", url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}) as response:
                    if response.status_code != 200:
                        raise ValueError("EXPORT_HTTP_REJECTED")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 32 * 1024 * 1024:
                            raise ValueError("EXPORT_TOO_LARGE")
                    results.append(json.loads(raw))
        return results
    return fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--export-url")
    source.add_argument("--export-file", type=Path)
    parser.add_argument("--categories-url")
    parser.add_argument("--categories-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.export_url:
        if not args.categories_url or args.categories_file:
            parser.error("URL export requires categories URL")
        fetch = validated_fetcher(args.export_url, args.categories_url, secret("BACKEND_SERVICE_TOKEN"))
    else:
        if not args.categories_file or args.categories_url:
            parser.error("File export requires categories file")
        def fetch():
            return json.loads(args.export_file.read_text()), json.loads(args.categories_file.read_text())
    database = os.environ["AI_DATABASE_NAME"]
    url = validate_dsn(secret("CATALOG_SYNC_DATABASE_URL"), database, "ai_catalog_sync")
    audit(url, database)
    root = Path(os.environ.get("SEARCH_RUNTIME_ROOT", "/runtime"))
    prepare_encoder(root, Path(os.environ.get("ASSET_ROOT", "/assets")))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "product-search"))
    from tools.sync_postgres import sync
    print(json.dumps(sync(root, url, fetch, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Catalog sync failed ({type(exc).__name__}); inspect PG state before retrying.", file=sys.stderr)
        raise SystemExit(1) from None
