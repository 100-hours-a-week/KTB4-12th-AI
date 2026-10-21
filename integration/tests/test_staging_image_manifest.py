"""Deployment receipts must never mix commits, environments or partial image sets."""
import copy
import unittest

from integration.tools.staging_image_manifest import REGISTRY, build_manifest


SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def receipts(environment="staging"):
    return [
        {"target": target, "source_sha": SHA, "tag": "main-" + SHA,
         "digest": DIGEST, "image": f"{REGISTRY}/seonjalal/{environment}/ai-{target}@{DIGEST}"}
        for target in ["search", "profiler", "chat", "migrate"]
    ]


class StagingImageManifestTests(unittest.TestCase):
    def test_complete_set_is_ordered_and_digest_pinned(self):
        result = build_manifest(receipts(), SHA)
        self.assertEqual(list(result["images"]), ["chat", "migrate", "profiler", "search"])
        self.assertEqual(result["source_sha"], SHA)
        self.assertEqual(result["environment"], "staging")

    def test_partial_set_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "four"):
            build_manifest(receipts()[:-1], SHA)

    def test_duplicate_target_is_rejected(self):
        items = receipts()
        items.append(copy.deepcopy(items[0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_manifest(items, SHA)

    def test_mixed_commits_are_rejected(self):
        items = receipts()
        items[0]["source_sha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "commit"):
            build_manifest(items, SHA)

    def test_wrong_environment_account_target_or_digest_is_rejected(self):
        for old, new in [("/staging/", "/production/"),
                         ("721744297924", "000000000000"),
                         ("ai-search", "ai-chat"),
                         (DIGEST, "sha256:" + "c" * 64)]:
            with self.subTest(new=new):
                items = receipts()
                items[0]["image"] = items[0]["image"].replace(old, new)
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    build_manifest(items, SHA)

    def test_unexpected_target_digest_tag_and_fields_are_rejected(self):
        for key, value in [("target", "console"), ("digest", "latest"),
                           ("tag", "latest"), ("secret", "must-not-be-in-receipt")]:
            with self.subTest(key=key):
                items = receipts()
                items[0][key] = value
                with self.assertRaises(ValueError):
                    build_manifest(items, SHA)

    def test_short_sha_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "full"):
            build_manifest(receipts(), SHA[:7])

    def test_production_manifest_uses_only_production_digests(self):
        result = build_manifest(receipts("production"), SHA, "production")
        self.assertEqual(result["environment"], "production")
        self.assertEqual(list(result["images"]), ["chat", "migrate", "profiler", "search"])
        with self.assertRaisesRegex(ValueError, "mismatch"):
            build_manifest(receipts(), SHA, "production")

    def test_unknown_environment_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Only staging or production"):
            build_manifest(receipts(), SHA, "develop")


if __name__ == "__main__":
    unittest.main()
