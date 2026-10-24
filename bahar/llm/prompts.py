"""Static prompt prefixes (Qwen2.5 ChatML). Each prefix is evaluated once at startup and its KV
state restored before every call (LlmRuntime.register_prefix), so a request only evaluates its
own suffix. Never put per-request data (dates, user text) in a prefix.

Few-shot outputs are built from ParsedQuery objects and serialised in the grammar's key order
(required keys `in_scope`, `lang` first, then the schema order, `location_native` last), with defaults omitted — the
same compact shape the grammar lets the model emit.
"""

import json
import re
from typing import Any, Final

from bahar.models import Lang, ParsedQuery

PARSER_PREFIX_NAME: Final = "parser"

_PARSER_SYSTEM = """\
Convert a question about an outdoor activity in India into JSON. The question may be English, \
Hinglish, Hindi or Marathi, with typos. Output only JSON. Omit every key the user did not give.
in_scope: true only if the user wants to do one of: running, walking, cycling, golf, cricket, \
football, tennis, hiking (trek, fort, hills), picnic, event (party, gathering, function), \
photography. Anything else (swimming, gym, food, shopping, weather only, news) is false.
lang: en | hinglish (Hindi in Latin letters) | hi (Hindi, Devanagari) | mr (Marathi)
activity: one of the activities above. activity_raw: the user's own words for it.
location_text: the place exactly as named, in English letters. Never guess a place.
radius_km: only if the user gives a distance.
time.day: today | tomorrow | day_after | weekday:mon..sun | this_weekend | next_weekend | \
next_n_days | date:YYYY-MM-DD. aaj=today, kal/उद्या=tomorrow, parso/परसों/परवा=day_after.
time.n_days: only with next_n_days.
time.part: early_morning | morning | midday | afternoon | evening | night. subah/सुबह/सकाळी=morning, \
dopahar=afternoon, shaam/शाम/संध्याकाळी=evening, raat=night.
time.hour: 0-23, 24-hour clock (shaam 7 baje = 19). time.around: sunrise | sunset.
profile.age_group: child (kids along) | senior (60+, elderly parents). \
profile.conditions: asthma | copd | heart | pregnancy | other. profile.group_size: number of people.
preferences: short phrases like "shade", "flat", "quiet", "near water".
commute: {"from","to"} when travelling from one place to another; give only the ends named.
location_native: only for Devanagari questions: the place copied exactly as written there."""


def _shot(query: str, parsed: ParsedQuery) -> str:
    out = parsed.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    native = {"location_native": out.pop("location_native")} if "location_native" in out else {}
    ordered: dict[str, Any] = {"in_scope": out.pop("in_scope"), "lang": out.pop("lang"), **out, **native}
    answer = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    return f"<|im_start|>user\n{query}<|im_end|>\n<|im_start|>assistant\n{answer}<|im_end|>\n"


def _p(**fields: Any) -> ParsedQuery:
    return ParsedQuery.model_validate(fields)


# 10 shots: en, hinglish, hi, mr, typos, out-of-scope, missing location, commute, and the time
# idioms kal shaam / parso subah / is weekend / 7 baje / sunset ke time. Places here are
# deliberately not reused in scripts/smoke_parse.py.
_SHOTS: Final = (
    ("planning a 10k run around Cubbon Park this saturday morning, I'm 64",
     _p(in_scope=True, lang="en", activity="running", activity_raw="10k run",
        location_text="Cubbon Park", time={"day": "weekday:sat", "part": "morning"},
        profile={"age_group": "senior"})),
    ("kal shaam bacchon ke saath Juhu beach pe picnic kaisa rahega?",
     _p(in_scope=True, lang="hinglish", activity="picnic", activity_raw="picnic",
        location_text="Juhu Beach", time={"day": "tomorrow", "part": "evening"},
        profile={"age_group": "child"})),
    ("परसों सुबह लोधी गार्डन के पास साइकिल चलानी है, मुझे दमा है",
     _p(in_scope=True, lang="hi", activity="cycling", activity_raw="साइकिल चलानी",
        location_text="Lodhi Garden", location_native="लोधी गार्डन",
        time={"day": "day_after", "part": "morning"},
        profile={"conditions": ["asthma"]})),
    ("उद्या संध्याकाळी शिवाजी पार्कला क्रिकेट खेळायला जायचं आहे",
     _p(in_scope=True, lang="mr", activity="cricket", activity_raw="क्रिकेट",
        location_text="Shivaji Park", location_native="शिवाजी पार्क",
        time={"day": "tomorrow", "part": "evening"})),
    ("is weekend frends ke saath trek krna h lonavla side, 5 log, easy trail chahiye",
     _p(in_scope=True, lang="hinglish", activity="hiking", activity_raw="trek",
        location_text="Lonavala", time={"day": "this_weekend"},
        profile={"group_size": 5}, preferences=["easy trail"])),
    ("aaj shaam 7 baje Koramangala mein tennis khelna hai, heart patient hu",
     _p(in_scope=True, lang="hinglish", activity="tennis", activity_raw="tennis khelna",
        location_text="Koramangala", time={"day": "today", "part": "evening", "hour": 19},
        profile={"conditions": ["heart"]})),
    ("sunset ke time Bandstand pe photos lene hain",
     _p(in_scope=True, lang="hinglish", activity="photography", activity_raw="photos lene",
        location_text="Bandstand", time={"around": "sunset"})),
    ("swimming pool kahan milega Andheri mein?",
     _p(in_scope=False, lang="hinglish", activity_raw="swimming", location_text="Andheri")),
    ("is it safe to go for a jog tomorrow at 6am? I have COPD",
     _p(in_scope=True, lang="en", activity="running", activity_raw="jog",
        time={"day": "tomorrow", "hour": 6}, profile={"conditions": ["copd"]})),
    ("want to cycle from Baner to Hinjewadi on 2nd Nov around 9am, shady roads pls",
     _p(in_scope=True, lang="en", activity="cycling", activity_raw="cycle",
        time={"day": "date:2026-11-02", "hour": 9}, preferences=["shade"],
        commute={"from": "Baner", "to": "Hinjewadi"})),
)  # fmt: skip

PARSER_PREFIX: Final = (
    f"<|im_start|>system\n{_PARSER_SYSTEM}<|im_end|>\n" + "".join(_shot(q, p) for q, p in _SHOTS)
)

_CONTROL = re.compile(r"<\|[^|>]*\|>")  # ChatML control tokens must not come from user text
MAX_QUERY_CHARS: Final = 400


def parser_suffix(query: str) -> str:
    text = _CONTROL.sub(" ", query)[:MAX_QUERY_CHARS].strip()
    return f"<|im_start|>user\n{text}<|im_end|>\n<|im_start|>assistant\n"


# ---------------------------------------------------------------- composer
# One prefix per language: a 1.5B model imitates the language and shape of the last few-shot,
# so every shot in a prefix is in that prefix's language (one no-safe-time case, then the
# common ok case last). Hindi / Marathi answers cost ~2x the tokens of Hinglish (a 5-line
# Hindi answer is ~290 tokens, over the 200-token cap), so those prefixes write only the
# recommendation and why; the lists come from templates (llm.composer.LANG_MODE).

_COMPOSER_RULES = """\
You write the answer for Bahar, which plans outdoor activities in India. Use only the facts \
given; add nothing else. Every time, date, number and place name in the facts is a reference \
like {{e3}} or {{v1}}: copy the reference exactly. Never write a digit, a number word or a \
place name yourself. Call PM2.5 "fine dust". Describe air quality and heat only through \
their category references (the one right after the number, like {{e4}}); never judge them in \
your own words (clean, fresh, hot, saaf, साफ). If the facts say NO SAFE TIME, say so first."""

_FULL_FORMAT = """\
Write in {lang}, one short line per section:
1. In bold: the best place and time (or: no safe time, and the least-bad option).
2. {why}: two or three reasons from the facts.
3. {carry}, 4. {watch}, 5. {avoid}: from the facts; leave a line out if it has no facts.
6. {why_not}: one line about the place in "Why not". If the facts say "Close call", instead \
write that the place is nearly as good, without a label.
Keep it under one hundred words."""

_SHORT_FORMAT = """\
Write in {lang}, exactly two lines:
1. In bold: the best place and time (or: no safe time, and the least-bad option).
2. {why}: two or three reasons from the facts."""

LANG_NAMES: Final[dict[Lang, str]] = {
    Lang.en: "English",
    Lang.hinglish: "Hinglish (Hindi in Latin letters)",
    Lang.hi: "Hindi (Devanagari script)",
    Lang.mr: "Marathi (Devanagari script)",
}
# why, carry, watch, avoid, why_not ("{v}" = the venue ref)
LABELS: Final[dict[Lang, tuple[str, str, str, str, str]]] = {
    Lang.en: ("Why", "Carry", "Watch for", "Avoid", "Why not {v}"),
    Lang.hinglish: ("Kyun", "Saath rakhein", "Dhyan rakhein", "Avoid karein", "{v} kyun nahi"),
    Lang.hi: ("क्यों", "साथ रखें", "ध्यान रखें", "इनसे बचें", "{v} क्यों नहीं"),
    Lang.mr: ("का", "सोबत ठेवा", "लक्ष ठेवा", "टाळा", "{v} का नाही"),
}
SHORT_LANGS: Final = frozenset({Lang.hi, Lang.mr})

_FACTS_NO_SAFE = """\
Activity: picnic · with children
Requested time: {{e1}}
Status: NO SAFE TIME in the requested window; least-bad option below
Least bad: {{v1}} (park), {{e2}}
Problems:
- heat-stress index {{e3}}, {{e4}}, at or above your stop level of {{e5}}
- UV index {{e6}}
Improves from: {{e7}}
Carry: water; electrolytes / ORS; sunscreen and a cap
Avoid: {{e8}} (heat stress)
Why not {{v2}} (garden): heat-stress index {{e9}}, {{e10}}, vs {{e3}} at the pick"""

_FACTS_OK = """\
Activity: running · asthma/COPD
Requested time: {{e1}}
Best: {{v1}} (park), {{e2}}
Why:
- fine dust {{e3}}, {{e4}}
- heat-stress index {{e5}}, {{e6}}
- green cover {{e7}} around it
Other place: {{v2}} (park), {{e8}} — traffic pollution blowing in from {{e9}}
Other time here: {{e10}} — heat-stress index {{e11}}, {{e12}}, vs {{e5}} at the pick
Carry: your reliever inhaler; an {{e13}} mask
Watch for: heat stress rises from {{e14}}; warm up slowly; stop if breathing gets hard
Avoid: {{e15}} (air above your limit)
Why not {{v3}} (garden): traffic pollution blowing in from {{e16}}"""

# (no-safe answer, ok answer) per language
_ANSWERS: Final[dict[Lang, tuple[str, str]]] = {
    Lang.en: ("""\
**There is no safe time in {{e1}}. The least-bad option is {{v1}}, {{e2}}.**
**Why:** {{e4}} ({{e3}}), above the safe level of {{e5}} for children, and UV {{e6}}. It gets better from {{e7}}.
**Carry:** water, ORS, sunscreen and a cap.
**Avoid:** {{e8}}, heat stress.
**Why not {{v2}}:** {{e10}} there ({{e9}}).""", """\
**Go to {{v1}}, {{e2}}.**
**Why:** fine dust {{e3}}, {{e4}}; {{e6}} ({{e5}}); and {{e7}} of the area around it is green.
**Carry:** your reliever inhaler and an {{e13}} mask.
**Watch for:** heat stress rising from {{e14}}; warm up slowly and stop if breathing gets hard.
**Avoid:** {{e15}}, air above your limit.
**Why not {{v3}}:** traffic pollution blows in from {{e16}}."""),
    Lang.hinglish: ("""\
**{{e1}} mein koi safe time nahi hai. Sabse kam risk: {{v1}}, {{e2}}.**
**Kyun:** {{e4}} ({{e3}}), bacchon ki safe limit {{e5}} se upar, aur UV {{e6}}. {{e7}} se haalat behtar hogi.
**Saath rakhein:** paani, ORS, sunscreen aur cap.
**Avoid karein:** {{e8}}, heat stress.
**{{v2}} kyun nahi:** wahan {{e10}} ({{e9}}), aur {{e11}} door (car se).""", """\
**{{v1}} best rahega, {{e2}}.**
**Kyun:** dhool {{e3}}, {{e4}}; {{e6}} ({{e5}}); aur aas-paas {{e7}} hariyali.
**Saath rakhein:** apna inhaler aur {{e13}} mask.
**Dhyan rakhein:** {{e14}} se heat stress badhega; dheere warm up kariye, saans phoole toh ruk jaiye.
**Avoid karein:** {{e15}}, hawa aapki limit se upar hai.
**{{v3}} kyun nahi:** wahan {{e16}} se traffic ka dhuaan aata hai."""),
    Lang.hi: ("""\
**{{e1}} में कोई सुरक्षित समय नहीं है। सबसे कम जोखिम: {{v1}}, {{e2}}।**
**क्यों:** {{e4}} ({{e3}}), जो बच्चों की सुरक्षित सीमा {{e5}} से ऊपर है। {{e7}} से हालात सुधरेंगे।""", """\
**{{v1}} सबसे अच्छा रहेगा, {{e2}}।**
**क्यों:** धूल {{e3}}, {{e4}}; {{e6}} ({{e5}}); और आसपास {{e7}} हरियाली।"""),
    Lang.mr: ("""\
**{{e1}} मध्ये सुरक्षित वेळ नाही. सर्वात कमी धोका: {{v1}}, {{e2}}.**
**का:** {{e4}} ({{e3}}), मुलांच्या सुरक्षित मर्यादेपेक्षा ({{e5}}) जास्त. {{e7}} पासून परिस्थिती सुधारेल.""", """\
**{{v1}} सर्वात चांगले, {{e2}}.**
**का:** धूळ {{e3}}, {{e4}}; {{e6}} ({{e5}}); आणि आजूबाजूला {{e7}} हिरवळ."""),
}  # fmt: skip


def composer_prefix_name(lang: Lang) -> str:
    return f"composer_{lang.value}"


def _user(facts: str, lang: Lang) -> str:
    return f"Language: {LANG_NAMES[lang]}\n{facts}"


def composer_prefix(lang: Lang) -> str:
    why, carry, watch, avoid, why_not = LABELS[lang]
    fmt = _SHORT_FORMAT if lang in SHORT_LANGS else _FULL_FORMAT
    system = _COMPOSER_RULES + "\n" + fmt.format(
        lang=LANG_NAMES[lang], why=why, carry=carry, watch=watch, avoid=avoid,
        why_not=why_not.format(v="..."),
    )  # fmt: skip
    no_safe, ok = _ANSWERS[lang]
    shots = ((_FACTS_NO_SAFE, no_safe), (_FACTS_OK, ok))
    return f"<|im_start|>system\n{system}<|im_end|>\n" + "".join(
        f"<|im_start|>user\n{_user(q, lang)}<|im_end|>\n<|im_start|>assistant\n{a}<|im_end|>\n"
        for q, a in shots
    )


SUMMARY_NOTE: Final = "Write only lines 1 and 2 (the recommendation and the reasons)."
RETRY_NOTE: Final = "Your previous answer was rejected ({problems}). Write it again using only the references."


def composer_suffix(facts: str, lang: Lang, *, summary: bool, problems: list[str] | None = None) -> str:
    notes = [SUMMARY_NOTE] * summary + ([RETRY_NOTE.format(problems="; ".join(problems))] if problems else [])
    text = "\n".join([_user(facts, lang), *notes])
    return f"<|im_start|>user\n{_CONTROL.sub(' ', text)}<|im_end|>\n<|im_start|>assistant\n"
