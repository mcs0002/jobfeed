"""Tests for the display-time job-description formatter (web/descfmt.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

from descfmt import format_description  # noqa: E402


def fmt(text, title=None):
    return str(format_description(text, title))


def test_empty_input():
    assert fmt("") == ""
    assert fmt(None) == ""
    assert fmt("   \n  \n") == ""


def test_cookie_and_consent_lines_dropped():
    src = (
        "We are hiring an analyst.\n"
        "Cookies\n"
        "We use cookies to improve your experience.\n"
        "You must accept the \"Social media\" cookies to see this content.\n"
        "The role is based in London."
    )
    out = fmt(src)
    assert "cookie" not in out.lower()
    assert "analyst" in out
    assert "London" in out


def test_real_gdpr_requirement_kept():
    # "cookie/consent" as artifact vs GDPR as a genuine skill must not be confused.
    src = "Responsibilities\nEnsure GDPR compliance across data pipelines."
    out = fmt(src)
    assert "GDPR" in out


def test_share_and_nav_chrome_dropped():
    src = (
        "Go to content\nGo to search\nBack to offers list\n"
        "Share this page!\nShare on LinkedIn, opens in a new tab\n"
        "Share on X (Twitter), opens in a new tab\n"
        "Join our markets team.\nApply now"
    )
    out = fmt(src)
    assert "Share on" not in out
    assert "Go to" not in out
    assert "Back to offers" not in out
    assert "markets team" in out
    assert "Apply now" not in out


def test_location_dump_removed():
    src = (
        "Same job available in 3 locations Atlanta, Georgia, United States\n"
        "Austin, Texas, United States\n"
        "Boston, Massachusetts, United States\n"
        "Position Summary\n"
        "We deliver strategic programs."
    )
    out = fmt(src)
    assert "Atlanta" not in out
    assert "Texas" not in out
    assert "Same job available" not in out
    assert "strategic programs" in out
    assert "<h4>Position Summary</h4>" in out


def test_leading_metadata_header_skipped():
    src = (
        "Apply as Client Advisor\n"
        "91512\n"
        "UniCredit Bulbank\n"
        "Retail Banking\n"
        "Bulgaria\n"
        "hr@unicredit.bg\n"
        "We offer a great internship programme.\n"
        "You will learn banking fundamentals."
    )
    out = fmt(src, title="Client Advisor")
    assert "91512" not in out
    assert "UniCredit Bulbank" not in out
    assert "hr@unicredit.bg" not in out
    assert "internship programme" in out


def test_trailing_similar_jobs_truncated():
    src = (
        "This is the actual job description.\n"
        "Other corresponding job offers\n"
        "Permanent HR Manager Lisbon, Portugal\n"
        "Permanent Analyst Brussels, Belgium"
    )
    out = fmt(src)
    assert "actual job description" in out
    assert "Lisbon" not in out
    assert "HR Manager" not in out


def test_bullets_become_list():
    src = (
        "Responsibilities\n"
        "● Build models\n"
        "• Talk to clients\n"
        "- Write reports"
    )
    out = fmt(src)
    assert out.count("<li>") == 3
    assert "<ul>" in out
    assert "Build models" in out
    assert "●" not in out  # marker stripped


def test_headings_detected():
    src = "Position Summary\nWe do things.\nKey Responsibilities\n• Do work"
    out = fmt(src)
    assert "<h4>Position Summary</h4>" in out
    assert "<h4>Key Responsibilities</h4>" in out


def test_paragraphs_grouped():
    src = "Line one of a paragraph\ncontinues here.\n\nA second paragraph."
    out = fmt(src)
    assert out.count("<p>") == 2


def test_title_repeat_line_dropped():
    src = (
        "Process Optimization Manager - - 310005 Process Optimization Manager\n"
        "This role drives efficiency."
    )
    out = fmt(src, title="Process Optimization Manager")
    assert "310005" not in out
    assert "drives efficiency" in out


def test_html_is_escaped():
    src = "Responsibilities\nUse <script>alert(1)</script> & \"quotes\" safely."
    out = fmt(src)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out


def test_no_raw_tags_leak_from_content():
    # Angle brackets in content never produce real tags beyond our own set.
    out = fmt("Requirements\nKnowledge of C++ <templates> and generics.")
    assert "<templates>" not in out
    assert "&lt;templates&gt;" in out


# ── Literal JSON-escape sequences (double-encoded ATS payloads) ──────────────
def test_literal_backslash_n_becomes_real_newlines():
    # A double-encoded ATS payload stores "\n" as two literal characters; a
    # real-looking sample from a bank posting: "Group\n\n Division" rendered
    # the backslashes verbatim before the fix.
    src = (
        "About Corporate & Investment Banking Group\\n\\n Global Markets Division\\n"
        "We are hiring an analyst for the trading desk.\\n\\n"
        "Responsibilities\\n- Price and execute client flow\\n- Monitor risk limits"
    )
    out = fmt(src)
    assert "\\n" not in out
    # Line structure was recovered: the heading and the bullets materialise.
    assert "<h4>Responsibilities</h4>" in out
    assert "<li>Price and execute client flow</li>" in out
    assert "<li>Monitor risk limits</li>" in out


def test_literal_escapes_fixed_in_clean_text_too():
    # tag.py's excerpt builder shares clean_text — the tagger must see real
    # lines, not backslash sequences.
    from descfmt import clean_text
    out = clean_text("We are hiring an analyst.\\n\\nThe role is based in London.\\tHybrid.")
    assert "\\n" not in out and "\\t" not in out
    assert "We are hiring an analyst.\n\nThe role is based in London. Hybrid." == out


def test_real_newlines_untouched_by_literal_unescape():
    src = "Responsibilities\nEnsure GDPR compliance across data pipelines."
    assert "GDPR" in fmt(src)
