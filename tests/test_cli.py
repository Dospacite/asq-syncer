from __future__ import annotations

import unittest

from master_list_creation.cli import DEFAULT_OUTPUT, _build, _parser, _update
from master_list_creation.validation import validate_master_list


class CliTest(unittest.TestCase):
    def test_update_defaults_to_the_application_asset(self) -> None:
        args = _parser().parse_args(["update"])

        self.assertEqual(args.output, DEFAULT_OUTPUT)
        self.assertEqual(args.cache_dir.name, ".cache")
        self.assertFalse(args.refresh)
        self.assertIs(args.handler, _update)

    def test_build_remains_available_for_alternate_outputs(self) -> None:
        args = _parser().parse_args(["build", "--output", "/tmp/sample.json"])

        self.assertEqual(str(args.output), "/tmp/sample.json")
        self.assertIs(args.handler, _build)

    def test_validator_accepts_source_version_and_rejects_unknown_fields(self) -> None:
        errors, _ = validate_master_list(
            {"animeMap": {}, "masterListId": "version", "unknown": True}
        )

        self.assertEqual(
            errors,
            ["top-level object contains unsupported field(s): unknown", "animeMap must be a non-empty object"],
        )


if __name__ == "__main__":
    unittest.main()
