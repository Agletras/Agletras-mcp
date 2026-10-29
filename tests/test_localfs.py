"""Tests for local path safety and folder scanning (Phase 2)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agletras_mcp import localfs
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import TempHomeCase


def _fresh_settings() -> "object":
    return load_settings()


class ResolveAllowedTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()  # roots include self.home by default

    def test_inside_root_resolves(self) -> None:
        target = self.home / "sub"
        target.mkdir()
        resolved = localfs.resolve_allowed(self.home / "sub" / ".." / "sub", self.settings)
        self.assertEqual(resolved, target.resolve())

    def test_outside_root_denied(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaises(ToolError) as ctx:
                localfs.resolve_allowed(Path(outside := str(Path(outside_tmp := tempfile.mkdtemp()))), self.settings)
            self.assertEqual(ctx.exception.code, "access_denied")

    def test_dotdot_escape_denied(self) -> None:
        # '..' from an in-roots dir must not resolve somewhere permitted-but-out
        nested = self.home / "nested"
        nested.mkdir()
        with self.assertRaises(ToolError) as ctx:
            localfs.resolve_allowed(nested / ".." / ".." / "..", self.settings)
        self.assertEqual(ctx.exception.code, "access_denied")

    def test_symlink_escape_denied(self) -> None:
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir)
            link = self.home / "escape"
            link_target = outside / "secret.txt"
            link_target.write_text("secret")
            link.symlink_to(link_target)
            with self.assertRaises(ToolError) as ctx:
                localfs.resolve_allowed(link, self.settings)
            self.assertEqual(ctx.exception.code, "access_denied")

    def test_missing_path_is_not_found(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            localfs.resolve_allowed(self.home / "nope.md", self.settings)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_no_roots_configured_denied(self) -> None:
        probe = self.home / "probe.txt"
        probe.write_text("x", encoding="utf-8")
        os.environ["AGLETRAS_LOCAL_ROOTS"] = ""
        try:
            with self.assertRaises(ToolError) as ctx:
                localfs.resolve_allowed(probe, load_settings())
            self.assertEqual(ctx.exception.code, "access_denied")
        finally:
            os.environ["AGLETRAS_LOCAL_ROOTS"] = str(self.home)


class ScanFolderTests(TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()
        (self.home / "docs").mkdir()
        (self.home / "docs" / "a.md").write_text("# a", encoding="utf-8")
        (self.home / "docs" / "b.txt").write_text("b", encoding="utf-8")
        (self.home / "docs" / "skip.tmp").write_text("temp", encoding="utf-8")
        (self.home / "docs" / ".hidden.md").write_text("hidden", encoding="utf-8")
        (self.home / "docs" / "data.csv").write_text("x,y\n1,2\n", encoding="utf-8")
        (self.home / "docs" / "prog.py").write_text("print('no')", encoding="utf-8")
        (self.home / "docs" / "sub").mkdir()
        (self.home / "docs" / "sub" / "c.md").write_text("# c", encoding="utf-8")

    def test_recursive_picks_supported_and_reports_skips(self) -> None:
        scan = localfs.scan_folder(self.home / "docs", self.settings)
        picked_names = {path.name for path in scan["picked"]}
        self.assertEqual(picked_names, {"a.md", "b.txt", "data.csv", "c.md"})
        reasons = {entry["path"].split("/")[-1]: entry["reason"] for entry in scan["skipped"]}
        self.assertEqual(reasons.get("skip.tmp"), "temporary_file")
        self.assertEqual(reasons.get(".hidden.md"), "hidden_or_backup_file")
        self.assertEqual(reasons.get("prog.py"), "unsupported_extension")
        self.assertFalse(scan["truncated"])

    def test_non_recursive_ignores_subdirs(self) -> None:
        scan = localfs.scan_folder(self.home / "docs", self.settings, recursive=False)
        picked_names = {path.name for path in scan["picked"]}
        self.assertEqual(picked_names, {"a.md", "b.txt", "data.csv"})

    def test_extension_allowlist(self) -> None:
        scan = localfs.scan_folder(
            self.home / "docs", self.settings, recursive=True, extensions=("csv", ".md")
        )
        picked_names = {path.name for path in scan["picked"]}
        self.assertEqual(picked_names, {"data.csv", "a.md", "c.md"})

    def test_max_files_cap_sets_truncated(self) -> None:
        scan = localfs.scan_folder(self.home / "docs", self.settings, recursive=True, max_files=2)
        self.assertEqual(len(scan["picked"]), 2)
        self.assertTrue(scan["truncated"])

    def test_symlink_to_outside_is_skipped_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir)
            (outside / "target.md").write_text("outside!", encoding="utf-8")
            link = self.home / "docs" / "evil.md"
            link.symlink_to(outside / "target.md")
            scan = localfs.scan_folder(self.home / "docs", self.settings)
            # the linked file's contents must not be collected
            self.assertNotIn(link, scan["picked"])
            reasons = [entry["reason"] for entry in scan["skipped"]]
            self.assertIn("symlink_outside_roots", reasons)

    def test_symlink_inside_tree_is_followed(self) -> None:
        link = self.home / "docs" / "alias.md"
        link.symlink_to(self.home / "docs" / "a.md")
        scan = localfs.scan_folder(self.home / "docs", self.settings)
        picked = [path for path in scan["picked"] if path.name == "alias.md"]
        self.assertEqual(len(picked), 1)

    def test_depth_cap(self) -> None:
        os.environ["AGLETRAS_FOLDER_MAX_DEPTH"] = "0"
        try:
            scan = localfs.scan_folder(self.home / "docs", load_settings(), recursive=True)
            picked_names = {path.name for path in scan["picked"]}
            self.assertNotIn("c.md", picked_names)
        finally:
            os.environ.pop("AGLETRAS_FOLDER_MAX_DEPTH", None)

    def test_oversized_file_reported_skipped(self) -> None:
        os.environ["AGLETRAS_FOLDER_MAX_FILE_BYTES"] = "2"
        try:
            scan = localfs.scan_folder(self.home / "docs", load_settings(), recursive=False)
            reasons = [entry["reason"] for entry in scan["skipped"]]
            self.assertIn("too_large", reasons)
        finally:
            os.environ.pop("AGLETRAS_FOLDER_MAX_FILE_BYTES", None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
