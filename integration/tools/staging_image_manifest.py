"""Require a complete, same-commit image set before handing it to Cloud CD."""
import argparse
import json
import re
from pathlib import Path


TARGETS = frozenset({"search", "profiler", "chat", "migrate"})
REGISTRY = "721744297924.dkr.ecr.ap-northeast-2.amazonaws.com"


def build_manifest(receipts, source_sha):
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("A full main commit SHA is required")
    images = {}
    for item in receipts:
        if not isinstance(item, dict) or set(item) != {
            "target", "source_sha", "tag", "digest", "image"
        }:
            raise ValueError("Unexpected image receipt fields")
        target = item["target"]
        if not isinstance(target, str) or target not in TARGETS or target in images:
            raise ValueError("Unknown or duplicate image target")
        if item["source_sha"] != source_sha or item["tag"] != "main-" + source_sha:
            raise ValueError("All image receipts must identify the requested commit")
        digest = item["digest"]
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Invalid image digest")
        if item["image"] != f"{REGISTRY}/seonjalal/staging/ai-{target}@{digest}":
            raise ValueError("Image registry, environment, target or digest mismatch")
        images[target] = item
    # 부분 발행은 재시도할 수 있지만, 네 이미지가 모두 모이기 전 배포 세트로 승인하지 않는다.
    if set(images) != TARGETS:
        raise ValueError("All four AI images are required")
    return {
        "schema_version": 1,
        "environment": "staging",
        "source_repository": "100-hours-a-week/KTB4-12th-AI",
        "source_sha": source_sha,
        "images": {target: images[target] for target in sorted(images)},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipts = [json.loads(path.read_text()) for path in sorted(args.receipts.glob("*.json"))]
    result = build_manifest(receipts, args.source_sha)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
