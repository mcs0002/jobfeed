"""
Deterministic posting-language detection — no LLM, no network.

detect_language(text) returns the dominant language of a job description as a
lowercase ISO-639-1 code from tag.LANG_CODES, or "" when the text is English,
too short, or ambiguous (e.g. a bilingual ad with a full English version).

Used three ways (all cheap):
- tag._build_payload adds a "PostingLanguage:" hint line so Haiku applies the
  "written in X ⇒ X required" rubric reliably;
- tag._tag_batch merges the detected code into lang_req as a hard backstop
  (a German-language ad must never end up filterable as "English only");
- backfill_tags --patch-desc-lang retro-patches rows tagged before this
  existed, without spending any quota.

Method: script-range checks first (CJK / Cyrillic / Arabic / Greek), then
stopword-frequency scoring for Latin-script languages, English included as the
null hypothesis. Deliberately conservative: below the thresholds we return ""
rather than guess — a miss here only means falling back to the LLM's answer.
"""
import re

# Top function words per language. Shared tokens (e.g. "de" in FR/ES/PT/NL) are
# fine — scoring is comparative across all sets at once, and each language's
# distinctive words dominate on real prose. English is scored too, as the
# baseline a candidate language must clearly beat.
_STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "of", "to", "in", "is", "you", "for", "with", "will",
           "are", "be", "we", "our", "as", "on", "this", "or", "an", "have",
           "your", "at", "from", "by", "that"},
    "de": {"der", "die", "das", "und", "für", "mit", "von", "sie", "wir",
           "ist", "ein", "eine", "einen", "den", "dem", "im", "zu", "zur",
           "auf", "bei", "sind", "oder", "werden", "sowie", "ihre", "als",
           "um", "nicht", "wie", "aus", "nach", "über", "unsere", "deine"},
    "fr": {"le", "la", "les", "des", "et", "du", "un", "une", "en", "vous",
           "nous", "pour", "avec", "dans", "est", "sont", "sur", "au", "aux",
           "vos", "nos", "être", "que", "qui", "plus", "votre", "notre",
           "ainsi", "cette", "afin", "chez", "ou", "par"},
    "es": {"el", "los", "las", "y", "en", "que", "con", "para", "por", "una",
           "un", "es", "del", "se", "como", "más", "nuestro", "nuestra",
           "será", "su", "sus", "trabajo", "equipo", "empresa", "años"},
    "it": {"il", "di", "e", "che", "per", "con", "un", "una", "del", "della",
           "delle", "dei", "le", "si", "sono", "più", "nel", "nella", "alla",
           "gli", "come", "anche", "lavoro", "nostro", "nostra"},
    "nl": {"het", "een", "en", "van", "voor", "met", "je", "wij", "bij",
           "aan", "op", "zijn", "worden", "niet", "ook", "onze", "als",
           "dat", "dit", "werken", "binnen", "naar", "onder", "over"},
    "pt": {"o", "os", "as", "e", "em", "que", "com", "para", "por", "uma",
           "um", "é", "do", "da", "dos", "das", "você", "nossa", "nosso",
           "será", "não", "mais", "como", "trabalho", "equipe", "área"},
    "pl": {"i", "w", "na", "z", "do", "się", "jest", "oraz", "dla", "nie",
           "jako", "przez", "będzie", "które", "który", "nasz", "naszej",
           "praca", "pracy", "zespołu", "od", "po", "lub", "aby"},
    "da": {"og", "i", "af", "til", "det", "en", "et", "der", "som", "på",
           "med", "for", "at", "du", "vi", "er", "din", "dine", "hos",
           "ikke", "vil", "kan", "eller", "vores", "arbejde"},
    "sv": {"och", "i", "av", "till", "det", "en", "ett", "som", "på", "med",
           "för", "att", "du", "vi", "är", "din", "dina", "hos", "inte",
           "kommer", "kan", "eller", "vår", "våra", "arbete"},
    "no": {"og", "i", "av", "til", "det", "en", "et", "der", "som", "på",
           "med", "for", "at", "du", "vi", "er", "din", "dine", "hos",
           "ikke", "vil", "kan", "eller", "vår", "våre", "arbeid"},
    "fi": {"ja", "on", "ei", "että", "joka", "sekä", "tai", "myös", "olemme",
           "sinä", "sinulla", "meillä", "työ", "työssä", "kanssa", "voit",
           "olet", "hyvä", "meidän", "sinun", "tehtävä", "tehtävässä"},
    "tr": {"ve", "bir", "için", "ile", "bu", "olarak", "olan", "çok", "gibi",
           "daha", "veya", "tüm", "bizim", "sizin", "takım", "çalışma",
           "deneyim", "yıl", "üzere", "kariyer"},
    "cs": {"a", "v", "na", "se", "je", "s", "pro", "jsou", "které", "který",
           "nebo", "jako", "budete", "náš", "naší", "práce", "týmu", "do",
           "z", "za", "aby", "při", "také"},
    "hu": {"és", "a", "az", "hogy", "is", "egy", "nem", "mint", "vagy",
           "csapat", "munka", "során", "valamint", "való", "vagyunk",
           "leszel", "velünk", "területén", "tapasztalat"},
    "ro": {"și", "în", "de", "la", "cu", "pentru", "este", "sunt", "care",
           "sau", "un", "o", "echipa", "noastră", "nostru", "vei", "din",
           "mai", "pe", "ani", "experiență"},
}

# Minimum evidence before we call a language at all.
_MIN_TOKENS = 25
_MIN_LETTERS = 60
# A candidate must hit at least this share of tokens AND clearly beat English.
_MIN_SCORE = 0.10
_BEAT_EN_FACTOR = 1.3

_WORD_RE = re.compile(r"[a-zà-öø-ÿāăąćčďęěğıłńňőřśšťůűźżžșț]+", re.IGNORECASE)

# (regex, code, share-of-letters threshold). Japanese before Chinese: JA prose
# mixes kana + Han, so any meaningful kana share decides JA.
_SCRIPTS = [
    (re.compile("[぀-ヿ]"), "ja", 0.05),   # hiragana + katakana
    (re.compile("[一-鿿]"), "zh", 0.20),   # Han
    (re.compile("[가-힯]"), "ko", 0.20),   # Hangul
    (re.compile("[Ѐ-ӿ]"), "ru", 0.30),   # Cyrillic
    (re.compile("[؀-ۿ]"), "ar", 0.30),   # Arabic
    (re.compile("[Ͱ-Ͽ]"), "el", 0.30),   # Greek
]


def detect_language(text: str | None) -> str:
    """Dominant non-English language of `text` ('' = English/unknown/ambiguous)."""
    if not text:
        return ""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < _MIN_LETTERS:
        return ""
    n_letters = len(letters)
    for rx, code, threshold in _SCRIPTS:
        if len(rx.findall(text)) / n_letters >= threshold:
            return code

    tokens = _WORD_RE.findall(text.lower())
    if len(tokens) < _MIN_TOKENS:
        return ""
    scores = {}
    for code, words in _STOPWORDS.items():
        scores[code] = sum(1 for t in tokens if t in words) / len(tokens)
    en = scores.pop("en")
    code, best = max(scores.items(), key=lambda kv: kv[1])
    if best >= _MIN_SCORE and best >= en * _BEAT_EN_FACTOR:
        return code
    return ""
