import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cover_letter


class CoverLetterContextTests(unittest.TestCase):
    def test_supporting_context_is_loaded_and_separated_in_payload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile_path = root / "profile.json"
            cv_path = root / "cv.txt"
            samples_path = root / "samples.txt"
            context_path = root / "context.txt"
            profile_path.write_text(json.dumps({
                "full_name": "Test Applicant",
                "career_narrative": "relevant experience",
                "current_role": "Student",
                "availability_narrative": "Available now",
            }))
            cv_path.write_text("CV facts")
            samples_path.write_text("Style sample")
            context_path.write_text("Verified supporting fact")

            with patch.multiple(
                cover_letter,
                PROFILE_JSON=os.fspath(profile_path),
                CV_TXT=os.fspath(cv_path),
                SAMPLES_TXT=os.fspath(samples_path),
                SUPPORTING_CONTEXT_TXT=os.fspath(context_path),
            ):
                profile, cv_text, samples = cover_letter._load_inputs()

            payload = cover_letter.build_payload(
                {"company": "Firm", "title": "Role", "description": "JD"},
                cv_text, profile, samples,
            )
            self.assertIn("Verified supporting fact", payload)
            self.assertIn("SUPPORTING-DOCUMENT NOTES", payload)
            self.assertIn("CV facts", payload)
            self.assertIn("Style sample", payload)


if __name__ == "__main__":
    unittest.main()
