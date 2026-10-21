"""Read-only compatibility for separately provisioned, typed Secrets.

Develop's single team JSON Secret is injected by the deployment into ordinary
environment variables instead. This legacy reader neither patches that JSON
nor writes any Secret. Dashboard edits use the shared private directory.
"""
from functools import lru_cache
import logging
import re

from integration.provider_key import KeyUnavailable


@lru_cache(maxsize=8)
def _client(region):
    import boto3
    from botocore.config import Config
    # SDK debug traces include request bodies, including SecretString.
    for name in {"boto3", "botocore", *logging.Logger.manager.loggerDict}:
        if name == "boto3" or name.startswith("boto3.") or name == "botocore" or name.startswith("botocore."):
            logging.getLogger(name).setLevel(logging.WARNING)
    return boto3.client("secretsmanager", region_name=region,
                        config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}))


def client(source):
    match = re.fullmatch(r"arn:aws(?:-us-gov|-cn)?:secretsmanager:([a-z0-9-]+):[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}", source.aws_secret_id)
    if not match:
        raise KeyUnavailable("AWS_CREDENTIAL_CONFIG_REQUIRED")
    try:
        return source.aws_client if source.aws_client is not None else _client(match[1])
    except Exception:
        raise KeyUnavailable("AWS_CREDENTIAL_STORE_UNAVAILABLE") from None


def read_current(source):
    try:
        result = client(source).get_secret_value(SecretId=source.aws_secret_id, VersionStage="AWSCURRENT")
        raw, version = result["SecretString"], result["VersionId"]
        if not isinstance(raw, str) or len(raw) > 4096 or "AWSCURRENT" not in result["VersionStages"]:
            raise ValueError()
        return source.parse_record(raw, version=version)
    except Exception:
        raise KeyUnavailable("AWS_CREDENTIAL_STORE_UNAVAILABLE") from None
