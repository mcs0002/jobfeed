import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from applications import browser


class StageDocumentsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "icloud"
        (self.source / "Sub").mkdir(parents=True)
        (self.source / "cv.pdf").write_bytes(b"%PDF-cv")
        (self.source / "Sub" / "t.pdf").write_bytes(b"%PDF-t")
        self.profile = self.root / "profile.json"
        self.profile.write_text(json.dumps({"documents": {
            "base_dir": "/staged/elsewhere",
            "source_dir": str(self.source),
            "cv": "cv.pdf",
            "transcript": "Sub/t.pdf",
            "transcript_note": "a note, not a file",
            "toefl": "missing.pdf",
        }}), encoding="utf-8")
        stage = patch.object(browser, "stage_root", return_value=self.root / "stage")
        stage.start()
        self.addCleanup(stage.stop)

    def test_copies_named_documents_from_source_dir_not_base_dir(self):
        out = browser.stage_documents(self.profile)
        staged = self.root / "stage" / "documents"
        self.assertEqual((staged / "cv.pdf").read_bytes(), b"%PDF-cv")
        self.assertEqual((staged / "Sub" / "t.pdf").read_bytes(), b"%PDF-t")
        self.assertEqual(sorted(out["copied"]), ["cv", "transcript"])
        self.assertEqual(out["missing"], ["toefl"])

    def test_staged_copy_is_a_real_file_and_restaging_is_idempotent(self):
        browser.stage_documents(self.profile)
        self.assertFalse((self.root / "stage" / "documents" / "cv.pdf").is_symlink())
        self.assertEqual(browser.stage_documents(self.profile)["copied"], [])

    def test_changed_source_is_restaged(self):
        browser.stage_documents(self.profile)
        cv = self.source / "cv.pdf"
        cv.write_bytes(b"%PDF-cv-v2")
        os.utime(cv, (1, 1))
        self.assertEqual(browser.stage_documents(self.profile)["copied"], ["cv"])
        self.assertEqual((self.root / "stage" / "documents" / "cv.pdf").read_bytes(), b"%PDF-cv-v2")


class PortFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "Chrome" / "DevToolsActivePort"
        p = patch.object(browser, "PORT_FILE", self.file)
        p.start()
        self.addCleanup(p.stop)
        self.info = {"webSocketDebuggerUrl": f"ws://127.0.0.1:{browser.PORT}/devtools/browser/abc"}

    def test_points_the_main_profile_file_at_the_dedicated_port(self):
        self.assertEqual(browser.ensure_port_file(self.info), "rewritten")
        self.assertEqual(self.file.read_text(), f"{browser.PORT}\n/devtools/browser/abc")
        self.assertEqual(browser.ensure_port_file(self.info), "unchanged")

    def test_overwrites_the_main_chromes_own_port(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("9222\n/devtools/browser/main")
        self.assertEqual(browser.ensure_port_file(self.info), "rewritten")
        self.assertTrue(self.file.read_text().startswith(f"{browser.PORT}\n"))


if __name__ == "__main__":
    unittest.main()
