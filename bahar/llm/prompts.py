"""Static prompt prefixes (Qwen2.5 ChatML). Each prefix is evaluated once at startup and its KV
state restored before every call (LlmRuntime.register_prefix), so a request only evaluates its
own suffix. Never put per-request data (dates, user text) in a prefix.

Few-shot outputs are built from ParsedQuery objects and serialised in the grammar's key order
(required keys `in_scope`, `lang` first, then the schema order), with defaults omitted — the
same compact shape the grammar lets the model emit.
"""

import json
import re
from typing import Any, Final

from bahar.models import ParsedQuery

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
commute: {"from","to"} when travelling from one place to another; give only the ends named."""


def _shot(query: str, parsed: ParsedQuery) -> str:
    out = parsed.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    ordered: dict[str, Any] = {"in_scope": out.pop("in_scope"), "lang": out.pop("lang"), **out}
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
        location_text="Lodhi Garden", time={"day": "day_after", "part": "morning"},
        profile={"conditions": ["asthma"]})),
    ("उद्या संध्याकाळी शिवाजी पार्कला क्रिकेट खेळायला जायचं आहे",
     _p(in_scope=True, lang="mr", activity="cricket", activity_raw="क्रिकेट",
        location_text="Shivaji Park", time={"day": "tomorrow", "part": "evening"})),
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
