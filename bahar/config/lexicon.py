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
# "right now": the window is the current hour and the 3 next (weights.NOW_WINDOW_H).
NOW: Final = re.compile(
    r"\b(right\s+now|just\s+now|this\s+moment|at\s+the\s+moment|abhi|ab|aatta|atta)\b|" + _dv("अभी|आत्ता|अब"), _I
)
# Day words the model's multi-day answers need support from in the question.
WEEKEND: Final = re.compile(r"\b(weekend|weekends|vikend|wknd)\b|" + _dv("वीकेंड|सप्ताहांत|शनि-रवि"), _I)
MULTI_DAY: Final = re.compile(
    r"\b(days|din|week|hafte|next\s+few)\b|" + _dv("दिन|दिवस|हफ्ते|आठवडा|आठवड्यात"), _I
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

# ---- Devanagari place names (parser._native): the SLM copies the place, often only its first
# word ("साल्ट" for "साल्ट लेक के पास"), so the copy is extended up to the next of these words,
# and Marathi suffixes glued to the last word are cut ("पाषाण तलावाजवळ" -> "पाषाण तलाव").
NATIVE_STOP_WORDS: Final = frozenset({
    "के", "की", "का", "में", "पर", "पे", "को", "से", "तक", "पास", "नज़दीक", "नजदीक", "और", "है",
    "हैं", "मे", "वर", "ला", "च्या", "जवळ", "मध्ये", "आणि", "आहे", "येथे", "इथे", "साठी",
})  # fmt: skip
NATIVE_SUFFIXES: Final = ("मध्ये", "जवळ", "च्या", "वरून", "पर्यंत", "समोर")
# When the model copies nothing: the place is the run of words right before the first
# locative postposition ("इंडिया गेट के पास", "अंधेरी में"), at most NATIVE_MAX_WORDS words.
NATIVE_LOCATIVE: Final = re.compile(_dv("के पास|के नज़दीक|के नजदीक|में|मे|पर|पे|जवळ|मध्ये"))
NATIVE_MAX_WORDS: Final = 3

# "after work" -> Part.after_work (start hours 17-19), unless an hour is given.
AFTER_WORK: Final = re.compile(
    r"\bafter\s+(work|office)\b|\b(office|kaam|kam|duty)\s+ke\s+baad\b|"
    + _dv("ऑफिस के बाद|दफ़्तर के बाद|दफ्तर के बाद|काम के बाद|ऑफिसनंतर|कामानंतर"),
    _I,
)

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
# A stated age: "65 saal", "65 साल", "65 years old", "age 65", "उम्र ६५". >= SENIOR_AGE ->
# senior, <= CHILD_AGE -> child. Not a duration: "2 saal se", "for 2 years" (parser checks).
AGE: Final = re.compile(
    r"(?<![\d.])(?P<n>\d{1,3})\s*-?\s*(?:saal|sal|years?|yrs?|yr|yo|y/o|varsh|साल|वर्ष|वर्षे|"
    r"वर्षांचा|वर्षांची|वर्षांचे)(?![\w\u0900-\u097F])"
    r"|(?:\bage[ds]?|\bumar|\bumr|उम्र|वय)\s*[:\-]?\s*(?:is\s+)?(?P<m>\d{1,3})(?!\d)",
    _I,
)
DURATION_AFTER: Final = re.compile(r"^\s*(se|से|पासून|since|ago|pehle|पहले)\b", _I)
DURATION_BEFORE: Final = re.compile(r"\b(for|since|last|past|pichhle|पिछले)\s*$", _I)
SENIOR_AGE: Final = 60  # CLAUDE.md profile table: senior (60+)
CHILD_AGE: Final = 12  # child (< 12); a stated 12 is treated as a child too (cautious)

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

# ---- bare place names (parser.recover_place): Latin words that are never part of a place name
# when the model found no location. Activities, days, parts of day, sun events and health /
# company words are excluded by their own lexicons; these are the remaining function words.
PLACE_STOP_WORDS: Final = frozenset({
    "a", "an", "the", "i", "my", "me", "we", "our", "us", "you", "to", "for", "of", "and", "or",
    "with", "in", "at", "on", "near", "nearby", "around", "by", "from", "into", "after",
    "before", "is", "are", "am", "can", "could", "should", "would", "will", "want", "need",
    "go", "going", "do", "does", "have", "has", "best", "good", "better", "nice", "safe",
    "where", "when", "what", "which", "how", "any", "some", "there", "here", "this", "that",
    "please", "plan", "place", "spot", "time", "today", "tomorrow", "morning", "evening",
    "night", "afternoon", "weekend", "photos", "photo", "pics", "shots", "ok", "okay",
})  # fmt: skip

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

# ---- grounding (bahar/llm/validator.py). A number word is rejected only next to a unit or a
# time word ("do ghante", "five km"), so "one of the best" or Hindi "ek" alone pass.
NUMBER_WORDS: Final = frozenset({
    # en
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "fifteen", "twenty", "thirty", "forty", "fifty", "sixty", "hundred",
    "half", "quarter", "couple",
    # hi / hinglish
    "ek", "do", "teen", "char", "chaar", "paanch", "panch", "chhe", "chhah", "saat", "aath",
    "nau", "das", "gyarah", "barah", "baarah", "pandrah", "bees", "tees", "chalis", "pachas",
    "sau", "dedh", "dhai", "dhaai", "sava", "saade", "sade", "aadha", "adha",
    "एक", "दो", "तीन", "चार", "पांच", "पाँच", "छह", "छः", "सात", "आठ", "नौ", "दस", "ग्यारह",
    "बारह", "पंद्रह", "बीस", "तीस", "चालीस", "पचास", "सौ", "डेढ़", "ढाई", "सवा", "साढ़े", "आधा",
    # mr
    "don", "paach", "saha", "daha", "दोन", "पाच", "सहा", "नऊ", "दहा", "अकरा", "बारा",
    "पंधरा", "वीस", "चाळीस", "पन्नास", "शंभर", "दीड", "अडीच", "साडे", "अर्धा",
})  # fmt: skip
UNIT_WORDS: Final = frozenset({
    "km", "kms", "kilometre", "kilometres", "kilometer", "kilometers", "m", "metre", "metres",
    "meter", "meters", "min", "mins", "minute", "minutes", "hour", "hours", "hr", "hrs", "%",
    "percent", "°", "°c", "degree", "degrees", "aqi", "µg", "ug", "mm", "am", "pm", "o'clock",
    "baje", "bje", "ghanta", "ghante", "ghanton", "minat", "pratishat", "vajta", "vaajta",
    "taas", "tas",
    "बजे", "घंटा", "घंटे", "घंटों", "मिनट", "किलोमीटर", "मीटर", "प्रतिशत", "डिग्री", "वाजता",
    "वाजे", "तास", "मिनिटे", "मिनिट", "किमी",
})  # fmt: skip
# Venue names made only of these words are generic ("Running Track", "Gymkhana") and may
# appear in prose; any other venue / place name must be written as its {{v#}} ref.
GENERIC_PLACE_WORDS: Final = frozenset({
    "park", "garden", "gardens", "track", "running", "jogging", "ground", "grounds",
    "playground", "pitch", "field", "court", "courts", "turf", "stadium", "beach", "lake",
    "gymkhana", "club", "the", "of", "public", "municipal", "children", "childrens", "play",
    "area", "open", "space", "recreation", "sports", "sport", "cricket", "football", "tennis",
    "golf", "course", "viewpoint", "fort", "hill", "hills", "temple", "jetty", "pier",
    "promenade", "walkway", "path", "trail", "cycle", "cycling", "lane", "road", "nature",
    "reserve", "forest", "picnic", "spot", "site", "point", "udyan", "maidan", "bagh",
})  # fmt: skip

# ---- grounded description (validator): air and heat may only be judged through their
# category refs. Clean-air words are rejected next to air worse than "satisfactory"; a heat
# word must not be followed by a value that is not a temperature / heat level / time.
CLEAN_AIR_WORDS: Final = re.compile(
    r"\b(clean|cleaner|fresh|pure|good\s+air|saaf|saf|swachh|shuddh|acchi\s+hawa|achhi\s+hawa|"
    r"achi\s+hawa)\b|" + _dv("साफ|साफ़|शुद्ध|स्वच्छ|ताजी|ताज़ी"),
    _I,
)
CLEAN_AIR_OK: Final = frozenset({"good", "satisfactory"})  # NAQI categories fit to call clean
HEAT_REF_WINDOW: Final = 4  # words after a heat word in which its value ref must appear
HEAT_WORDS: Final = frozenset({
    "heat", "hot", "hotter", "temperature", "garmi", "garam", "tapman",
    "गर्मी", "गरमी", "गरम", "तापमान", "उष्णता", "उष्णतेचा", "ऊष्मा", "उकाडा",
})  # fmt: skip
