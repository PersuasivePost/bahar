"""Word lists the parser's deterministic post-normalisation uses to check and correct the
SLM (bahar/llm/parser.py). They only cover what the user literally said: days, sun events,
distances, health conditions, and who is coming along.

Language: detection is deterministic (script + marker words) and overrides the parser's `lang`:
a 1.5B model mislabels short Hinglish as English too often to trust. Devanagari text is Hindi
unless it contains a Marathi marker; Latin text is English unless it contains a Hinglish (or
romanised Marathi) marker. Markers are common function words and time words that do not
occur in English or in the other language.
"""

import re
from typing import Final

DEVANAGARI: Final = re.compile(r"[ऀ-ॿ]")

# Marathi words absent from Hindi (Hindi equivalents: है, कल, मुझे, कहाँ, और, सुबह, शाम, पास…).
MARATHI_DEVANAGARI: Final = frozenset({
    "आहे", "आहेत", "उद्या", "परवा", "मला", "आम्हाला", "आम्ही", "कुठे", "कुठं", "आणि", "नाही",
    "जायचं", "जायचे", "करायचं", "करायची", "करायला", "खेळायला", "फिरायला", "चालायला",
    "धावायला", "सकाळी", "संध्याकाळी", "दुपारी", "रात्री", "कसं", "कसे", "चांगलं", "जवळ",
    "साठी", "मध्ये", "आहोत",
})  # fmt: skip

MARATHI_LATIN: Final = frozenset({
    "udya", "sakali", "sandhyakali", "aahe", "ahe", "kuthe", "jaycha", "jaaycha", "karaycha",
    "khelayla", "firayla", "chalayla", "mala", "amhi", "ani",
})  # fmt: skip

HINGLISH: Final = frozenset({
    "hai", "hain", "hu", "hoon", "kal", "aaj", "parso", "parson", "kahan", "kaha", "kidhar",
    "karu", "karun", "karna", "krna", "karni", "karne", "ke", "ka", "ki", "ko", "mein", "pe",
    "paas", "saath", "sath", "chahiye", "baje", "shaam", "sham", "subah", "raat", "dopahar",
    "kya", "kaisa", "kaise", "kaun", "kaunsa", "accha", "acha", "jaana", "jana", "jaun",
    "hoga", "rahega", "mujhe", "humko", "hum", "yaar", "bhai", "wala", "wali", "liye", "tak",
    "aur", "nahi", "abhi", "thoda", "log", "bacche", "bachchon", "dost", "doston", "khelna",
    "khelne", "ghoomne", "ghumne", "daudna", "kheechne", "lene", "hoti", "hota",
})  # fmt: skip

_I = re.IGNORECASE


def _dv(words: str) -> str:
    """Whole-word Devanagari alternatives (\\b does not work: vowel signs are not \\w)."""
    return rf"(?<![\u0900-\u097F])(?:{words})(?![\u0900-\u097F])"


# ---- days. An explicit day word beats the model's day (see parser._said_day for precedence).
DAY_AFTER: Final = re.compile(r"day\s+after|\bparso\b|\bparson\b|" + _dv("परसों|परवा"), _I)
TODAY: Final = re.compile(
    r"\b(aaj|aj|today|tonight)\b|\bthis\s+(morning|afternoon|evening)\b|" + _dv("आज"), _I
)
TOMORROW: Final = re.compile(
    r"\b(kal|tomorrow|tmrw|tmr|tomorow|tommorow|udya)\b|" + _dv("कल|उद्या"), _I
)
WEEKDAYS: Final[dict[str, re.Pattern[str]]] = {
    "mon": re.compile(r"\b(monday|somvar|somwar)\b|सोमवार", _I),
    "tue": re.compile(r"\b(tuesday|mangalvar|mangalwar)\b|मंगलवार|मंगळवार", _I),
    "wed": re.compile(r"\b(wednesday|budhvar|budhwar)\b|बुधवार", _I),
    "thu": re.compile(r"\b(thursday|guruvar|guruwar)\b|गुरुवार|वीरवार", _I),
    "fri": re.compile(r"\b(friday|shukravar|shukrawar)\b|शुक्रवार", _I),
    "sat": re.compile(r"\b(saturday|shanivar|shaniwar)\b|शनिवार", _I),
    "sun": re.compile(r"\b(sunday|ravivar|raviwar|itvaar|itwar)\b|रविवार|इतवार", _I),
}
# A month name means the model's date: may be real; then day words do not override it.
MONTH: Final = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|जनवरी|फरवरी|मार्च|"
    r"अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|अक्टूबर|नवंबर|दिसंबर",
    _I,
)

# ---- part of day. Overrides the model's part only when it contradicts the word.
PART_WORDS: Final[dict[str, re.Pattern[str]]] = {
    "morning": re.compile(r"\b(subah|subha)\b|" + _dv("सुबह|सकाळी"), _I),
    "afternoon": re.compile(r"\b(dopahar|dupahar)\b|" + _dv("दोपहर|दुपारी"), _I),
    "evening": re.compile(r"\b(shaam|sham)\b|" + _dv("शाम|संध्याकाळी"), _I),
    "night": re.compile(r"\b(raat|rat)\b|" + _dv("रात|रात्री"), _I),
}
# Model parts compatible with each word ("subah jaldi" may be early_morning).
PART_COMPATIBLE: Final[dict[str, frozenset[str]]] = {
    "morning": frozenset({"morning", "early_morning"}),
    "afternoon": frozenset({"afternoon", "midday"}),
    "evening": frozenset({"evening"}),
    "night": frozenset({"night"}),
}
# "<part> N baje" on a 12-hour clock: hours up to PM_MAX_HOUR shift by 12 for evening / night
# ("raat 9 baje" = 21:00); afternoon shifts only 1-6 ("dopahar 2 baje" = 14:00, "11 baje" = 11).
_BAJE = r"(?:baje|bje|बजे|वाजता|वाजे)"
PART_HOUR: Final[dict[str, re.Pattern[str]]] = {
    part: re.compile(rf"(?:{pat.pattern})\s*(?:ko\s+|को\s+)?(?P<h>\d{{1,2}})\s*{_BAJE}", _I)
    for part, pat in PART_WORDS.items()
}
PM_MAX_HOUR: Final = 11
AFTERNOON_PM_MAX_HOUR: Final = 6

SUNRISE: Final = re.compile(r"sun\s*rise|सूर्योदय", _I)
SUNSET: Final = re.compile(r"sun\s*set|सूर्यास्त", _I)

# "within 30 km", "20 km ke andar" — a bare "10 km run" is a distance to run, not a radius.
RADIUS: Final = re.compile(
    r"(?:within|under|up\s*to|upto|radius|max)\s*(\d+(?:\.\d+)?)\s*(?:km|kms|kilomet)"
    r"|(\d+(?:\.\d+)?)\s*(?:km|kms)\s*(?:ke\s+andar|tak|radius|away|range)",
    _I,
)

# ---- location clean-up: prepositions the model copies into the place name.
LOCATION_NOISE: Final = re.compile(
    r"^(near|around|in|at|close\s+to)\s+|[\s,]+(near|side|area|ke\s+pa+s|mein|me|pe|par)$", _I
)

# ---- health and company. Conditions said in the query are added to the parser's; the model's
# age group and "other" condition are kept only when the query has a matching cue.
CONDITION_PHRASES: Final[dict[str, re.Pattern[str]]] = {
    "asthma": re.compile(r"\basthma|\bdamaa?\b|दमा|अस्थमा|\binhaler", _I),
    "copd": re.compile(r"\bcopd\b|bronchitis", _I),
    "heart": re.compile(
        r"\bheart\s+(patient|problem|condition|disease)|\bcardiac|हृदय|दिल\s+के\s+मरीज", _I
    ),
    "pregnancy": re.compile(r"\bpregnan|garbhvati|गर्भवती|गरोदर", _I),
}
OTHER_CONDITION_CUE: Final = re.compile(
    r"\b(patient|problem|condition|disease|bimari|beemari|allerg\w*|diabet\w*|bp|surgery|"
    r"injur\w*|knee|arthritis|migraine|sick|unwell|recovering|operation)\b|बीमार|बिमारी|आजार|"
    r"तकलीफ|त्रास",
    _I,
)
CHILD_CUE: Final = re.compile(
    r"\b(kids?|child|children|toddlers?|baby|babies|son|daughter|bacch?e|bachch?e|bachchon|"
    r"bacchon|beta|beti)\b|बच्च|मुले|मुलां|मुलगा|मुलगी",
    _I,
)
SENIOR_CUE: Final = re.compile(
    r"\b(senior|elderly|old|aged|retired|grand\w*|buzurg|bujurg|dadi|dada|nani|nana)\b|"
    r"\b[6-9]\d\s*(\+|yrs?|years?|yo)|बुज़ुर्ग|बुजुर्ग|बूढ़|आजी|आजोबा|वृद्ध|म्हातार",
    _I,
)
# Words that make the profile senior even when the model missed it (stricter limits apply).
SENIOR_SAID: Final = re.compile(
    r"\b(elderly|buzurg|bujurg)\b|बुज़ुर्ग|बुजुर्ग|वृद्ध|म्हातारे|म्हातारी|म्हातारा", _I
)

# ---- scope. Activities Bahar clearly does not cover: a query naming one of these (and none
# of ours) is out of scope; an activity that is merely unrecognised gets a clarifying question.
UNSUPPORTED_ACTIVITY: Final = re.compile(
    r"\b(gym|workout|swim\w*|pool|movies?|films?|cinema|theatre|shopping|mall|restaurants?|"
    r"cafe|biryani|food|dinner|lunch|breakfast|pubs?|bar|clubbing|bowling|badminton|"
    r"basketball|volleyball|kabaddi|skating|yoga|zumba|dance|dancing|concert\s+tickets?)\b|"
    + _dv("जिम|तैराकी|स्विमिंग|फिल्म|फ़िल्म|मूवी|सिनेमा|शॉपिंग|खरीदारी|खाना|रेस्टोरेंट|योग|"
          "पोहणे|पोहायला|खरेदी|जेवण|बॅडमिंटन|बैडमिंटन"),
    _I,
)

# ---- cities: a city named anywhere in the query is added to the geocoder search string
# ("Andheri ... Mumbai" -> "Andheri, Mumbai"). Alias -> canonical name; longest alias first.
CITIES: Final[dict[str, str]] = {
    "navi mumbai": "Navi Mumbai", "mumbai": "Mumbai", "bombay": "Mumbai", "मुंबई": "Mumbai",
    "pune": "Pune", "poona": "Pune", "पुणे": "Pune",
    "new delhi": "New Delhi", "delhi": "Delhi", "दिल्ली": "Delhi",
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "blr": "Bengaluru",
    "hyderabad": "Hyderabad", "chennai": "Chennai", "madras": "Chennai",
    "kolkata": "Kolkata", "calcutta": "Kolkata", "कोलकाता": "Kolkata",
    "ahmedabad": "Ahmedabad", "gurugram": "Gurugram", "gurgaon": "Gurugram", "noida": "Noida",
    "thane": "Thane", "ठाणे": "Thane", "chandigarh": "Chandigarh", "jaipur": "Jaipur",
    "lucknow": "Lucknow", "kochi": "Kochi", "nagpur": "Nagpur", "नागपूर": "Nagpur",
    "nashik": "Nashik", "नाशिक": "Nashik", "indore": "Indore", "bhopal": "Bhopal",
}  # fmt: skip
CITY: Final = re.compile(
    "|".join(
        rf"\b{re.escape(a)}\b" if a.isascii() else _dv(re.escape(a))
        for a in sorted(CITIES, key=len, reverse=True)
    ),
    _I,
)
