import unittest

import requests

from scrapers._http import fix_encoding, fix_encoding_utf8

# UTF-8 names that turn into mojibake when a UTF-8 body is decoded as Latin-1.
SAMPLE = "Nestlé Qualitäts Crédit Agricole Estagiário Société Générale"


def _response(body: str, content_type: str, encoding=...):
    r = requests.Response()
    r.status_code = 200
    r.headers["Content-Type"] = content_type
    r._content = body.encode("utf-8")
    if encoding is not ...:
        r.encoding = encoding
    return r


class FixEncodingTests(unittest.TestCase):
    def test_corrects_latin1_fallback_to_utf8(self):
        # The production condition: requests resolved ISO-8859-1, so .text
        # would mojibake. fix_encoding must re-detect UTF-8.
        r = _response(SAMPLE, "text/html", encoding="ISO-8859-1")
        self.assertNotEqual(r.text, SAMPLE)  # mojibake before
        fix_encoding(r)
        self.assertEqual(r.encoding.lower(), "utf-8")
        self.assertEqual(r.text, SAMPLE)

    def test_corrects_when_encoding_is_none(self):
        r = _response(SAMPLE, "text/html", encoding=None)
        fix_encoding(r)
        self.assertEqual(r.text, SAMPLE)

    def test_leaves_declared_utf8_untouched(self):
        r = _response(SAMPLE, "text/html; charset=utf-8", encoding="utf-8")
        fix_encoding(r)
        self.assertEqual(r.encoding, "utf-8")
        self.assertEqual(r.text, SAMPLE)

    def test_force_utf8_on_latin1_fallback(self):
        r = _response(SAMPLE, "text/javascript", encoding="ISO-8859-1")
        fix_encoding_utf8(r)
        self.assertEqual(r.encoding, "utf-8")
        self.assertEqual(r.text, SAMPLE)

    def test_force_utf8_respects_declared_charset(self):
        r = _response(SAMPLE, "text/html; charset=windows-1252", encoding="windows-1252")
        fix_encoding_utf8(r)
        self.assertEqual(r.encoding, "windows-1252")

    def test_returns_same_response(self):
        r = _response(SAMPLE, "text/html", encoding="ISO-8859-1")
        self.assertIs(fix_encoding(r), r)


if __name__ == "__main__":
    unittest.main()
