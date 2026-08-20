"""Posting-language detection + the lang_req merge backstop.

The detector must be conservative: English and ambiguous/bilingual text return
'' (fall back to the LLM's answer); clear foreign-language prose returns the
code. Samples are realistic job-ad prose, not word salads."""
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lang_detect import detect_language
from tag import merge_detected_lang

GERMAN = (
    "Für unser Team in Frankfurt suchen wir einen Analysten. Sie arbeiten eng "
    "mit den Händlern zusammen und unterstützen die Strukturierung von "
    "Zinsprodukten. Wir bieten eine attraktive Vergütung sowie flexible "
    "Arbeitszeiten. Ihre Aufgaben umfassen die Analyse von Marktdaten, die "
    "Erstellung von Präsentationen und die Betreuung unserer Kunden. Sie "
    "verfügen über ein abgeschlossenes Studium der Wirtschaftswissenschaften "
    "und haben erste Erfahrungen im Kapitalmarktgeschäft gesammelt."
)

FRENCH = (
    "Au sein de la direction des marchés, vous participerez à la structuration "
    "des produits de taux pour nos clients institutionnels. Vous serez en "
    "charge de l'analyse des données de marché et de la préparation des "
    "présentations. Nous recherchons un profil avec une première expérience "
    "dans le domaine des marchés financiers. Vous êtes diplômé d'une grande "
    "école de commerce ou d'ingénieur et vous avez une forte appétence pour "
    "les produits dérivés."
)

ENGLISH = (
    "We are looking for an analyst to join our rates structuring team in "
    "Frankfurt. You will work closely with traders and sales to develop "
    "solutions for institutional clients. The ideal candidate has a degree in "
    "finance or economics and strong analytical skills. We offer a "
    "competitive compensation package and excellent career development "
    "opportunities within our global markets division."
)

CHINESE = (
    "我们正在寻找一名分析师加入我们的利率结构团队。您将与交易员和销售密切合作，"
    "为机构客户开发解决方案。理想的候选人拥有金融或经济学学位，具备较强的分析能力。"
    "我们提供有竞争力的薪酬和良好的职业发展机会。工作地点在上海，需要良好的中文沟通能力。"
)


class DetectLanguageTests(unittest.TestCase):
    def test_german(self):
        self.assertEqual(detect_language(GERMAN), "de")

    def test_french(self):
        self.assertEqual(detect_language(FRENCH), "fr")

    def test_english_returns_empty(self):
        self.assertEqual(detect_language(ENGLISH), "")

    def test_chinese_script(self):
        self.assertEqual(detect_language(CHINESE), "zh")

    def test_short_text_ambiguous(self):
        self.assertEqual(detect_language("Analyst gesucht in Frankfurt"), "")

    def test_empty_and_none(self):
        self.assertEqual(detect_language(""), "")
        self.assertEqual(detect_language(None), "")

    def test_bilingual_balanced_is_ambiguous(self):
        # A full English version alongside the German one → don't call it.
        self.assertEqual(detect_language(GERMAN + "\n\n" + ENGLISH + "\n" + ENGLISH), "")


class MergeDetectedLangTests(unittest.TestCase):
    def test_adds_detected_to_empty(self):
        self.assertEqual(merge_detected_lang("", "de"), "de")

    def test_adds_detected_to_existing(self):
        self.assertEqual(merge_detected_lang("fr", "de"), "de,fr")

    def test_already_present_unchanged(self):
        self.assertEqual(merge_detected_lang("de,fr", "de"), "de,fr")

    def test_no_detection_is_noop(self):
        self.assertEqual(merge_detected_lang("fr", ""), "fr")
        self.assertEqual(merge_detected_lang("", ""), "")

    def test_none_stays_none(self):
        # A failed tag leaves lang_req NULL; the backstop must not turn that
        # into a value (NULL drives the nightly re-tag selection).
        self.assertIsNone(merge_detected_lang(None, "de"))


if __name__ == "__main__":
    unittest.main()
