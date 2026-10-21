"""Unagreed BE full-snapshot proof. Missing products are deletions only after proof."""
import copy
import hashlib
import json
from uuid import UUID


def digest(products, categories):
    raw = json.dumps({"products": products, "categories": categories["data"]["categories"]},
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def complete(export, categories):
    result = copy.deepcopy(export)
    proof = result.get("data", {}).pop("snapshot", None)
    if not isinstance(proof, dict) or set(proof) != {"id", "complete", "productCount", "sha256"}:
        raise ValueError("COMPLETE_SNAPSHOT_PROOF_REQUIRED")
    if proof["complete"] is not True or type(proof["productCount"]) is not int:
        raise ValueError("INCOMPLETE_SNAPSHOT")
    if proof["productCount"] != len(result["data"]["products"]):
        raise ValueError("SNAPSHOT_COUNT_MISMATCH")
    ident = str(UUID(proof["id"]))
    expected = digest(result["data"]["products"], categories)
    if proof["sha256"] != expected:
        raise ValueError("SNAPSHOT_DIGEST_MISMATCH")
    return result, ident, expected
