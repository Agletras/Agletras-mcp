"""Tests for the Phase 7 configuration file, precedence, and redaction."""

from __future__ import annotations

import os
import unittest

from agletras_mcp.config import (
    DEPTH_PRESETS,
    ConfigError,
    default_config_path,
    load_branding,
    load_profiles,
    load_settings,
    settings_snapshot,
    validate_settings,
)

from tests.support import TempHomeCase


class ConfigFileTests(TempHomeCase):
    """Config file parsing, precedence, and validation."""

    def write_config(self, text: str, name: str = "agletras.toml"):
        path = self.home / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_explicit_config_file_is_an_error(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_settings(config_path=self.home / "nope.toml")
        self.assertIn("not found", str(caught.exception))

    def test_default_config_file_is_loaded_from_home(self) -> None:
        path = self.write_config(
            """
[fetch]
max_chars = 12345

[paths]
data_dir = "custom-data"
"""
        )
        self.assertEqual(default_config_path(), self.home / "agletras.toml")
        settings = load_settings()
        self.assertEqual(settings.fetch_max_chars, 12345)
        # Relative config paths resolve against the config file's directory.
        self.assertEqual(settings.data_dir, self.home / "custom-data")
        self.assertEqual(settings.config_path, path)
        self.assertEqual(settings.db_path, self.home / "custom-data" / "research.db")

    def test_agletras_config_env_selects_the_file(self) -> None:
        path = self.write_config("[fetch]\ntimeout = 9.5\n", name="custom.toml")
        os.environ["AGLETRAS_CONFIG"] = str(path)
        settings = load_settings()
        self.assertEqual(settings.fetch_timeout, 9.5)
        self.assertEqual(settings.config_path, path)

    def test_precedence_cli_over_env_over_file_over_defaults(self) -> None:
        self.write_config("[fetch]\nmax_chars = 11111\n")
        # File beats the built-in default.
        self.assertEqual(load_settings().fetch_max_chars, 11111)
        # Environment beats the file.
        os.environ["AGLETRAS_FETCH_MAX_CHARS"] = "22222"
        self.assertEqual(load_settings().fetch_max_chars, 22222)
        # Explicit overrides (the CLI's channel) beat the environment.
        settings = load_settings(overrides={"fetch_max_chars": 33333})
        self.assertEqual(settings.fetch_max_chars, 33333)

    def test_unknown_sections_and_keys_are_rejected(self) -> None:
        path = self.write_config("[nope]\nx = 1\n")
        with self.assertRaises(ConfigError) as caught:
            load_settings(config_path=path)
        self.assertIn("nope", str(caught.exception))

        path = self.write_config("[fetch]\nnot_a_key = 1\n")
        with self.assertRaises(ConfigError) as caught:
            load_settings(config_path=path)
        self.assertIn("not_a_key", str(caught.exception))

    def test_wrong_types_are_rejected(self) -> None:
        path = self.write_config('[fetch]\ntimeout = "soon"\n')
        with self.assertRaises(ConfigError):
            load_settings(config_path=path)

        path = self.write_config("[security]\nallow_private_hosts = 3\n")
        with self.assertRaises(ConfigError):
            load_settings(config_path=path)

    def test_invalid_toml_is_rejected(self) -> None:
        path = self.write_config("[fetch\ntimeout = 1\n")
        with self.assertRaises(ConfigError) as caught:
            load_settings(config_path=path)
        self.assertIn("TOML", str(caught.exception))

    def test_unknown_override_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(overrides={"nonsense": 1})

    def test_research_section_drives_the_pipeline_defaults(self) -> None:
        self.write_config(
            """
[research]
depth = "thorough"
max_sources = 7
followup_rounds = 0
verification_method = "deterministic"
include_images = true
"""
        )
        settings = load_settings()
        self.assertEqual(settings.research_depth, "thorough")
        self.assertEqual(settings.research_max_sources, 7)
        self.assertEqual(settings.research_followup_rounds, 0)
        self.assertEqual(settings.research_verification_method, "deterministic")
        self.assertTrue(settings.research_include_images)
        self.assertTrue(validate_settings(settings)["ok"])

    def test_negative_followup_rounds_other_than_minus_one_invalid(self) -> None:
        settings = load_settings(overrides={"research_followup_rounds": -5})
        report = validate_settings(settings)
        self.assertFalse(report["ok"])
        self.assertTrue(any("followup_rounds" in item for item in report["errors"]))

    def test_json_config_style_task_key(self) -> None:
        """The TOML section/key layout matches the documented example."""
        path = self.write_config(
            """
[logging]
format = "json"
file = ""

[research]
resume_max_age_days = 3
"""
        )
        settings = load_settings(config_path=path)
        self.assertEqual(settings.log_format, "json")
        self.assertEqual(settings.research_resume_max_age_days, 3)
        self.assertTrue(validate_settings(settings)["ok"])


class RedactionTests(TempHomeCase):
    def test_snapshot_masks_secret_named_settings(self) -> None:
        settings = load_settings()
        snapshot = settings_snapshot(settings)
        for name in ("vision_api_key_env", "verification_api_key_env"):
            self.assertEqual(snapshot[name], "***redacted***", name)
        # Non-secret values stay visible.
        self.assertEqual(snapshot["search_provider"], settings.search_provider)
        self.assertIsInstance(snapshot["data_dir"], str)

    def test_snapshot_is_json_serializable(self) -> None:
        import json

        json.dumps(settings_snapshot(load_settings()))


class DepthPresetTests(TempHomeCase):
    def test_presets_are_ordered_and_bounded(self) -> None:
        quick, standard, thorough = (
            DEPTH_PRESETS["quick"],
            DEPTH_PRESETS["standard"],
            DEPTH_PRESETS["thorough"],
        )
        self.assertLessEqual(quick["max_sources"], standard["max_sources"])
        self.assertLessEqual(standard["max_sources"], thorough["max_sources"])
        self.assertLessEqual(quick["followup_rounds"], standard["followup_rounds"])
        self.assertLessEqual(standard["followup_rounds"], thorough["followup_rounds"])
        self.assertEqual(quick["followup_rounds"], 0)

    def test_unknown_depth_is_an_invalid_configuration(self) -> None:
        settings = load_settings(overrides={"research_depth": "extreme"})
        report = validate_settings(settings)
        self.assertFalse(report["ok"])
        self.assertTrue(any("depth" in item for item in report["errors"]))

    def test_invalid_log_format_is_reported(self) -> None:
        settings = load_settings(overrides={"log_format": "xml"})
        self.assertFalse(validate_settings(settings)["ok"])


class ProfileTests(TempHomeCase):
    """Phase 8 ``[profiles.<name>]`` presets and ``[branding]``."""

    def write_config(self, text: str) -> None:
        (self.home / "agletras.toml").write_text(text, encoding="utf-8")

    def test_no_config_file_means_no_profiles_and_no_branding(self) -> None:
        self.assertEqual(load_profiles(), {})
        self.assertEqual(load_branding(), {})

    def test_profile_values_round_trip_exactly(self) -> None:
        self.write_config(
            """
[profiles.quick-check]
depth = "quick"
max_sources = 5
max_claims = 11
searches_per_subquestion = 1
followup_rounds = 0
results_per_search = 3
verification_method = "deterministic"
include_images = false
image_results = 1
parallel = true
reuse_evidence = true
project = "demo"
title = "Quick check"
"""
        )
        profiles = load_profiles()
        self.assertEqual(
            profiles["quick-check"],
            {
                "depth": "quick",
                "max_sources": 5,
                "max_claims": 11,
                "searches_per_subquestion": 1,
                "followup_rounds": 0,
                "results_per_search": 3,
                "verification_method": "deterministic",
                "include_images": False,
                "image_results": 1,
                "parallel": True,
                "reuse_evidence": True,
                "project": "demo",
                "title": "Quick check",
            },
        )

    def test_profiles_do_not_change_settings(self) -> None:
        self.write_config(
            """
[profiles.quick]
depth = "quick"
max_sources = 5

[research]
max_sources = 12
"""
        )
        settings = load_settings()
        self.assertEqual(settings.research_max_sources, 12)
        self.assertEqual(load_profiles()["quick"]["max_sources"], 5)

    def test_followup_rounds_minus_one_is_allowed_but_lower_is_not(self) -> None:
        self.write_config('[profiles.p]\nfollowup_rounds = -1\n')
        self.assertEqual(load_profiles()["p"]["followup_rounds"], -1)
        self.write_config('[profiles.p]\nfollowup_rounds = -2\n')
        with self.assertRaises(ConfigError) as caught:
            load_profiles()
        self.assertIn("followup_rounds", str(caught.exception))

    def test_negative_int_profile_values_are_rejected(self) -> None:
        self.write_config('[profiles.p]\nmax_sources = -1\n')
        with self.assertRaises(ConfigError) as caught:
            load_profiles()
        self.assertIn(">= 0", str(caught.exception))

    def test_profile_type_errors_are_rejected(self) -> None:
        cases = (
            '[profiles.p]\nmax_sources = "5"\n',
            '[profiles.p]\nmax_sources = true\n',
            '[profiles.p]\nparallel = "yes"\n',
            '[profiles.p]\ndepth = 3\n',
        )
        for text in cases:
            with self.subTest(text=text):
                self.write_config(text)
                with self.assertRaises(ConfigError):
                    load_profiles()

    def test_profile_enum_values_are_validated(self) -> None:
        self.write_config('[profiles.p]\ndepth = "extreme"\n')
        with self.assertRaises(ConfigError) as caught:
            load_profiles()
        self.assertIn("depth", str(caught.exception))
        self.write_config('[profiles.p]\nverification_method = "vibes"\n')
        with self.assertRaises(ConfigError) as caught:
            load_profiles()
        self.assertIn("verification_method", str(caught.exception))

    def test_unknown_profile_key_is_rejected(self) -> None:
        self.write_config('[profiles.p]\nmagic = true\n')
        with self.assertRaises(ConfigError) as caught:
            load_profiles()
        self.assertIn("unknown key profiles.p.magic", str(caught.exception))

    def test_profiles_section_must_hold_tables(self) -> None:
        self.write_config("[profiles]\nquick = 3\n")
        with self.assertRaises(ConfigError) as caught:
            load_profiles()
        self.assertIn("[profiles.quick]", str(caught.exception))

    def test_blank_profile_name_is_rejected(self) -> None:
        self.write_config('[profiles."  "]\nmax_sources = 1\n')
        with self.assertRaises(ConfigError) as caught:
            load_profiles()
        self.assertIn("profile names", str(caught.exception))

    def test_branding_round_trip(self) -> None:
        self.write_config(
            """
[branding]
name = "Acme Research"
primary_color = "#1a2B3c"
footer = "internal draft"
"""
        )
        self.assertEqual(
            load_branding(),
            {"name": "Acme Research", "primary_color": "#1a2B3c", "footer": "internal draft"},
        )

    def test_branding_unknown_key_and_type_errors(self) -> None:
        self.write_config("[branding]\nlogo = \"x.png\"\n")
        with self.assertRaises(ConfigError) as caught:
            load_branding()
        self.assertIn("unknown key branding.logo", str(caught.exception))
        self.write_config("[branding]\nname = 7\n")
        with self.assertRaises(ConfigError):
            load_branding()

    def test_branding_name_must_not_be_empty_and_color_must_be_hex(self) -> None:
        self.write_config('[branding]\nname = "   "\n')
        with self.assertRaises(ConfigError) as caught:
            load_branding()
        self.assertIn("must not be empty", str(caught.exception))
        self.write_config('[branding]\nprimary_color = "blue"\n')
        with self.assertRaises(ConfigError) as caught:
            load_branding()
        self.assertIn("#rrggbb", str(caught.exception))

    def test_unknown_sections_are_still_rejected_with_profiles_present(self) -> None:
        self.write_config('[profiles.p]\nmax_sources = 1\n\n[nope]\nx = 1\n')
        with self.assertRaises(ConfigError) as caught:
            load_profiles()
        self.assertIn("unknown section [nope]", str(caught.exception))

    def test_missing_explicit_config_path_raises_for_meta_loaders(self) -> None:
        missing = self.home / "absent.toml"
        for loader in (load_profiles, load_branding):
            with self.assertRaises(ConfigError) as caught:
                loader(missing)
            self.assertIn("not found", str(caught.exception))

    def test_settings_load_with_profiles_and_branding_present(self) -> None:
        self.write_config(
            """
[branding]
name = "Acme"

[profiles.quick]
depth = "quick"

[research]
max_claims = 7
"""
        )
        settings = load_settings()
        self.assertEqual(settings.research_max_claims, 7)
        self.assertTrue(validate_settings(settings)["ok"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
