"""Deterministic user-facing text in the user's language: the gate and status messages, the
phrases for every fact (English for the composer's facts block, English + Hinglish for the
answer), and the template answer used when the SLM is unavailable or fails validation.

Phrases contain refs ({{e#}} / {{v#}}), never digits; render.text fills them in."""

import re
from typing import Final

from bahar.models import FactItem, FactSheet, Lang, SlotFacts

ACTIVITY_LIST: Final[dict[Lang, str]] = {
    Lang.en: "running, walking, cycling, golf, cricket, football, tennis, hiking, picnics, "
    "outdoor events and photography",
    Lang.hinglish: "running, walking, cycling, golf, cricket, football, tennis, trekking, "
    "picnic, outdoor events aur photography",
    Lang.hi: "दौड़, सैर, साइकिलिंग, गोल्फ़, क्रिकेट, फ़ुटबॉल, टेनिस, ट्रेकिंग, पिकनिक, "
    "बाहरी कार्यक्रम और फ़ोटोग्राफ़ी",
    Lang.mr: "धावणे, चालणे, सायकलिंग, गोल्फ, क्रिकेट, फुटबॉल, टेनिस, ट्रेकिंग, सहल, "
    "बाहेरचे कार्यक्रम आणि फोटोग्राफी",
}

OUT_OF_SCOPE: Final[dict[Lang, str]] = {
    Lang.en: "Sorry, Bahar only plans outdoor activities: {activities}. Tell me which one and "
    "where, and I'll find the best place and time.",
    Lang.hinglish: "Sorry, Bahar sirf outdoor activities plan karta hai: {activities}. Bataiye "
    "kaunsi activity aur kahan, toh best jagah aur time mil jayega.",
    Lang.hi: "माफ़ कीजिए, बहार सिर्फ़ बाहरी गतिविधियों की योजना बनाता है: {activities}। बताइए "
    "कौन-सी गतिविधि और कहाँ, तो सबसे अच्छी जगह और समय मिल जाएगा।",
    Lang.mr: "माफ करा, बहार फक्त बाहेरच्या उपक्रमांचे नियोजन करते: {activities}. कोणता उपक्रम "
    "आणि कुठे ते सांगा, म्हणजे सर्वात चांगली जागा आणि वेळ सांगता येईल.",
}

# Exactly one question each.
ASK_LOCATION: Final[dict[Lang, str]] = {
    Lang.en: "Which area or landmark should I search near?",
    Lang.hinglish: "Kis area ya landmark ke paas dhoondhun?",
    Lang.hi: "किस इलाके या जगह के पास ढूँढूँ?",
    Lang.mr: "कोणत्या भागाजवळ किंवा ठिकाणाजवळ शोधू?",
}

ASK_ACTIVITY: Final[dict[Lang, str]] = {
    Lang.en: "Which activity do you have in mind: {activities}?",
    Lang.hinglish: "Kaunsi activity karni hai: {activities}?",
    Lang.hi: "आप कौन-सी गतिविधि करना चाहते हैं: {activities}?",
    Lang.mr: "तुम्हाला कोणता उपक्रम करायचा आहे: {activities}?",
}

ASK_COMMUTE_FROM: Final[dict[Lang, str]] = {
    Lang.en: "Where will you be starting from?",
    Lang.hinglish: "Aap kahan se nikloge?",
    Lang.hi: "आप कहाँ से निकलेंगे?",
    Lang.mr: "तुम्ही कुठून निघणार?",
}

ASK_COMMUTE_TO: Final[dict[Lang, str]] = {
    Lang.en: "Where are you heading to?",
    Lang.hinglish: "Kahan tak jaana hai?",
    Lang.hi: "आपको कहाँ तक जाना है?",
    Lang.mr: "तुम्हाला कुठपर्यंत जायचं आहे?",
}


def out_of_scope(lang: Lang) -> str:
    return OUT_OF_SCOPE[lang].format(activities=ACTIVITY_LIST[lang])


def ask_activity(lang: Lang) -> str:
    return ASK_ACTIVITY[lang].format(activities=ACTIVITY_LIST[lang])


PLACE_NOT_FOUND: Final[dict[Lang, str]] = {
    Lang.en: "I couldn't find that place on the map. Which area or landmark should I search near?",
    Lang.hinglish: "Yeh jagah map par nahi mili. Kis area ya landmark ke paas dhoondhun?",
    Lang.hi: "यह जगह नक्शे पर नहीं मिली। किस इलाके या जगह के पास ढूँढूँ?",
    Lang.mr: "ही जागा नकाशावर सापडली नाही. कोणत्या भागाजवळ किंवा ठिकाणाजवळ शोधू?",
}
NO_VENUES: Final[dict[Lang, str]] = {
    Lang.en: "I couldn't find any mapped places for {activity} near there. Try a bigger area "
    "or a nearby landmark.",
    Lang.hinglish: "Wahan ke paas {activity} ke liye koi mapped jagah nahi mili. Thoda bada area "
    "ya paas ka landmark try kariye.",
    Lang.hi: "वहाँ के पास {activity} के लिए कोई जगह नहीं मिली। कोई बड़ा इलाका या पास की जगह बताइए।",
    Lang.mr: "तिथे जवळ {activity} साठी कोणतीही जागा सापडली नाही. मोठा भाग किंवा जवळचे ठिकाण सांगा.",
}
NO_FORECAST: Final[dict[Lang, str]] = {
    Lang.en: "The weather forecast is unavailable right now, so I can't confirm a safe time. "
    "Please try again in a few minutes.",
    Lang.hinglish: "Abhi weather forecast nahi mil raha, isliye safe time confirm nahi kar sakte. "
    "Thodi der baad try kariye.",
    Lang.hi: "अभी मौसम का पूर्वानुमान नहीं मिल रहा, इसलिए सुरक्षित समय नहीं बता सकते। थोड़ी देर "
    "बाद फिर कोशिश करें।",
    Lang.mr: "सध्या हवामानाचा अंदाज मिळत नाही, त्यामुळे सुरक्षित वेळ सांगता येत नाही. थोड्या "
    "वेळाने पुन्हा प्रयत्न करा.",
}
INTERNAL_ERROR: Final[dict[Lang, str]] = {
    Lang.en: "Sorry, something went wrong while checking this. Please try again.",
    Lang.hinglish: "Sorry, check karte waqt kuch gadbad ho gayi. Dobara try kariye.",
    Lang.hi: "माफ़ कीजिए, जाँच करते समय कुछ गड़बड़ हो गई। फिर से कोशिश करें।",
    Lang.mr: "माफ करा, तपासताना काहीतरी चूक झाली. पुन्हा प्रयत्न करा.",
}
BUSY: Final = (
    "Too many questions from your address: Bahar answers {n} a minute. Please wait a moment "
    "and try again."
)

# Footer notes (render.text appends them under the answer).
MODELLED_NOTE: Final[dict[Lang, str]] = {
    Lang.en: "Air-quality values are modelled estimates for the venue, not measurements.",
    Lang.hinglish: "Air quality ke numbers venue ke liye modelled estimate hain, measurement nahi.",
    Lang.hi: "वायु गुणवत्ता के आँकड़े स्थल के लिए मॉडल किए गए अनुमान हैं, माप नहीं।",
    Lang.mr: "हवेच्या गुणवत्तेचे आकडे ठिकाणासाठी मॉडेल केलेले अंदाज आहेत, मोजमाप नाही.",
}
DISCLAIMER: Final[dict[Lang, str]] = {
    Lang.en: "Not medical advice — follow your doctor's guidance.",
    Lang.hinglish: "Yeh medical advice nahi hai — apne doctor ki salah maaniye.",
    Lang.hi: "यह चिकित्सा सलाह नहीं है — अपने डॉक्टर की सलाह मानें।",
    Lang.mr: "हा वैद्यकीय सल्ला नाही — तुमच्या डॉक्टरांचा सल्ला पाळा.",
}
SOURCES_LABEL: Final[dict[Lang, str]] = {
    Lang.en: "Sources", Lang.hinglish: "Sources", Lang.hi: "स्रोत", Lang.mr: "स्रोत",
}  # fmt: skip

# Languages the template answer is written in; others fall back (CLAUDE.md grounding contract).
TEMPLATE_LANG: Final[dict[Lang, Lang]] = {
    Lang.en: Lang.en, Lang.hinglish: Lang.hinglish, Lang.hi: Lang.hinglish, Lang.mr: Lang.en,
}

# ---------------------------------------------------------------- fact phrases
# Key: code ("!" suffix = hard gate, "carry:" / "watch:" = advice). Each value lists phrasings
# in order of preference; the first whose {placeholders} are all available is used.
# {word} is the code-chosen qualitative word; other placeholders are evidence refs.

_EN: Final[dict[str, tuple[str, ...]]] = {
    "air_quality!": ("fine dust {value}, {category}, above your limit of {limit}",
                     "fine dust {value}, above your limit of {limit}"),
    "air_quality": ("fine dust {value}, {category}, vs {best} at the pick", "fine dust {value}, {category}",
                    "fine dust {value}"),
    "heat!": ("heat-stress index {value}, {category}, at or above your stop level of {limit}",
              "heat-stress index {value}, at or above your stop level of {limit}"),
    "heat": ("heat-stress index {value}, {category}, vs {best} at the pick",
             "heat-stress index {value}, {category}", "heat-stress index {value}"),
    "inhaled_dose": ("inhaled dust about {value} of a resting day's dose",),
    "travel_time": ("{value} by car vs {best} for the pick", "{value} away by car"),
    "rush_hour": ("{value} by car in rush hour vs {best} for the pick", "{value} by car in rush hour"),
    "thunderstorm": ("{what} forecast around {time}",),
    "storm_unstable": ("thunderstorm risk: unstable air, rain chance {rain}",),
    "rain!": ("heavy rain {value}",),
    "rain": ("rain chance {value}",),
    "wind!": ("wind too strong: {value}, gusts {gust}",),
    "wind": ("wind {value}, gusts {gust}",),
    "darkness!": ("dark at that time (sunrise {sunrise}, sunset {sunset})",),
    "darkness": ("in the dark (sunrise {sunrise}, sunset {sunset})",),
    "uv": ("UV index {value}",),
    "wet_ground!": ("pitch waterlogged after {value} of rain",),
    "wet_ground": ("wet ground after {value} of rain",),
    "green_cooling": ("green cover {value} around it",),
    "golden_hour": ("{word}-hour light {value}",),
    "cloud_colour": ("cloud cover {value} to catch the colour",),
    "closed!": ("closed at that time (hours: {value})",),
    "outside_radius!": ("outside your search area ({value} away)",),
    "access": ("members / private access ({value}): check entry",),
    "profile": ("hard effort in the heat is risky for you",),
    "pollution_exposure": ("traffic pollution blowing in from {source}",
                           "pollution sources upwind (exposure index {exposure})"),
    "carry:water": ("water",),
    "carry:electrolytes": ("electrolytes / ORS",),
    "carry:sunscreen_cap": ("sunscreen and a cap",),
    "carry:rain_gear": ("a rain jacket or umbrella",),
    "carry:inhaler": ("your reliever inhaler",),
    "carry:n95": ("an {mask} mask",),
    "carry:light_gear": ("a light or reflective gear",),
    "carry:insect_repellent": ("insect repellent",),
    "carry:grip_shoes": ("shoes with good grip",),
    "watch:wind_shift": ("wind turns from {from} to {to} around {time}",),
    "watch:heat_rising": ("heat stress rises from {time}",),
    "watch:storm_risk": ("thunderstorm forecast around {time}",),
    "watch:storm_unstable": ("thunderstorm risk (unstable air)",),
    "watch:wet_ground": ("wet ground",),
    "watch:rush_hour": ("rush-hour traffic on the road",),
    "watch:lit_paths": ("stick to lit paths after sunset ({sunset})",),
    "watch:warm_up": ("warm up slowly; stop if breathing gets hard",),
    "watch:shorter_sessions": ("keep it short, with rest breaks",),
    "watch:ease_off_in_heat": ("keep the effort easy in the heat",),
    "avoid:heat": ("heat stress",),
    "avoid:air_quality": ("poor air",),
    "avoid:darkness": ("dark",),
    "avoid:rain": ("heavy rain",),
    "avoid:wind": ("strong wind",),
    "avoid:thunderstorm": ("thunderstorm",),
    "avoid:closed": ("closed",),
}  # fmt: skip

_HINGLISH: Final[dict[str, tuple[str, ...]]] = {
    "air_quality!": ("hawa mein dhool {value}, {category}, aapki limit {limit} se zyada",
                     "hawa mein dhool {value}, aapki limit {limit} se zyada"),
    "air_quality": ("hawa mein dhool {value}, {category}; best jagah par {best}",
                    "hawa mein dhool {value}, {category}", "hawa mein dhool {value}"),
    "heat!": ("garmi ka index {value}, {category}, aapke stop level {limit} se upar",
              "garmi ka index {value}, aapke stop level {limit} se upar"),
    "heat": ("garmi ka index {value}, {category}; best jagah par {best}",
             "garmi ka index {value}, {category}", "garmi ka index {value}"),
    "inhaled_dose": ("saans mein dhool ek aaram ke din ka lagbhag {value}",),
    "travel_time": ("car se {value}, best jagah {best}", "car se {value} door"),
    "rush_hour": ("rush hour mein car se {value}",),
    "thunderstorm": ("{time} ke aas-paas {what} ka forecast",),
    "storm_unstable": ("toofan ka risk: baarish ka chance {rain}",),
    "rain!": ("tez baarish {value}",),
    "rain": ("baarish ka chance {value}",),
    "wind!": ("bahut tez hawa: {value}, jhonke {gust}",),
    "wind": ("hawa {value}, jhonke {gust}",),
    "darkness!": ("us waqt andhera (sunrise {sunrise}, sunset {sunset})",),
    "darkness": ("andhere mein (sunrise {sunrise}, sunset {sunset})",),
    "uv": ("UV index {value}",),
    "wet_ground!": ("{value} baarish ke baad pitch geela",),
    "wet_ground": ("{value} baarish ke baad zameen geeli",),
    "green_cooling": ("aas-paas {value} hariyali",),
    "golden_hour": ("{word} hour ki roshni {value}",),
    "cloud_colour": ("{value} badal, rang ke liye accha",),
    "closed!": ("us waqt band (timing: {value})",),
    "outside_radius!": ("search area se bahar ({value} door)",),
    "access": ("members / private access ({value}): entry check kariye",),
    "profile": ("garmi mein zyada mehnat aapke liye risky",),
    "pollution_exposure": ("{source} se traffic ka dhuaan aa raha", "hawa ki taraf pollution (index {exposure})"),
    "carry:water": ("paani",),
    "carry:electrolytes": ("electrolytes / ORS",),
    "carry:sunscreen_cap": ("sunscreen aur cap",),
    "carry:rain_gear": ("rain jacket ya chhata",),
    "carry:inhaler": ("apna reliever inhaler",),
    "carry:n95": ("{mask} mask",),
    "carry:light_gear": ("light ya reflective gear",),
    "carry:insect_repellent": ("machhar bhagane wali cream",),
    "carry:grip_shoes": ("achhi grip wale joote",),
    "watch:wind_shift": ("{time} ke aas-paas hawa {from} se {to} ho jayegi",),
    "watch:heat_rising": ("{time} se garmi badhegi",),
    "watch:storm_risk": ("{time} ke aas-paas toofan ka forecast",),
    "watch:storm_unstable": ("toofan ka risk",),
    "watch:wet_ground": ("geeli zameen",),
    "watch:rush_hour": ("raste mein rush-hour traffic",),
    "watch:lit_paths": ("sunset ({sunset}) ke baad roshni wale raste par rahiye",),
    "watch:warm_up": ("dheere warm up kariye; saans phoole toh ruk jaiye",),
    "watch:shorter_sessions": ("chhota rakhiye, beech mein aaram",),
    "watch:ease_off_in_heat": ("garmi mein effort halka rakhiye",),
    "avoid:heat": ("garmi",),
    "avoid:air_quality": ("kharab hawa",),
    "avoid:darkness": ("andhera",),
    "avoid:rain": ("tez baarish",),
    "avoid:wind": ("tez hawa",),
    "avoid:thunderstorm": ("toofan",),
    "avoid:closed": ("band",),
}  # fmt: skip

PHRASES: Final[dict[Lang, dict[str, tuple[str, ...]]]] = {Lang.en: _EN, Lang.hinglish: _HINGLISH}

PROFILE_WORDS: Final[dict[Lang, dict[str, str]]] = {
    Lang.en: {"asthma_copd": "asthma/COPD", "heart": "heart condition", "senior": "senior",
              "child": "with children", "pregnancy": "pregnant"},
    Lang.hinglish: {"asthma_copd": "asthma/COPD", "heart": "heart patient", "senior": "senior",
                    "child": "bacchon ke saath", "pregnancy": "pregnancy"},
}  # fmt: skip

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_DIGIT = re.compile(r"\d")


def phrase(item: FactItem, lang: Lang, kind: str = "") -> str | None:
    """The first phrasing whose placeholders are all available, refs filled as {{e#}}. Falls
    back to the English table, then to the engine label when it has no digit; else None."""
    keys = [f"{kind}:{item.code}"] if kind else [item.code + "!"] * item.gate + [item.code]
    values = {**{k: f"{{{{{v}}}}}" for k, v in item.refs.items()}, "word": item.word}
    for table in (PHRASES[lang], _EN):
        for text in next((table[k] for k in keys if k in table), ()):
            if all(values.get(n) for n in _PLACEHOLDER.findall(text)):
                out = _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)
                if item.word == "lacks":
                    out = ("without " if lang is Lang.en else "nahi: ") + out
                elif item.word == "less" and item.code == "green_cooling":
                    out = ("less " if lang is Lang.en else "kam ") + out
                return out
    return item.label if item.label and not _DIGIT.search(item.label) else None


def phrases(items: list[FactItem], lang: Lang, kind: str = "") -> list[str]:
    return [p for it in items if (p := phrase(it, lang, kind))]


def when(slot: SlotFacts) -> str:
    return f"{{{{{slot.when}}}}}"


def avoid_phrase(item: FactItem, lang: Lang) -> str | None:
    if "when" not in item.refs:
        return None
    span = f"{{{{{item.refs['when']}}}}}"
    reason = phrase(item, lang, "avoid")
    return f"{span} ({reason})" if reason else span


# ---------------------------------------------------------------- template answer

_LABELS: Final[dict[Lang, dict[str, str]]] = {
    Lang.en: {"best": "Best", "least_bad": "No safe time in {window}. Least-bad option",
              "why": "Why", "problem": "Problem", "carry": "Carry", "watch": "Watch for",
              "avoid": "Avoid", "other": "Other options", "or": "or", "here": "here",
              "why_not": "Why not {venue}", "improves": "Conditions improve from {time}.",
              "no_improve": "No safe start found in the following two days either."},
    Lang.hinglish: {"best": "Sabse accha", "least_bad": "{window} mein koi safe time nahi. Sabse kam bura option",
                    "why": "Kyun", "problem": "Dikkat", "carry": "Saath rakhein",
                    "watch": "Dhyan rakhein", "avoid": "Avoid karein", "other": "Aur options",
                    "or": "ya", "here": "yahin", "why_not": "{venue} kyun nahi",
                    "improves": "Haalat {time} se behtar hogi.",
                    "no_improve": "Agle do din mein bhi koi safe time nahi mila."},
}  # fmt: skip


def answer_lines(sheet: FactSheet, lang: Lang) -> list[str]:
    """The deterministic answer, one markdown line per section, with refs."""
    return head_lines(sheet, lang) + tail_lines(sheet, lang)


def head_lines(sheet: FactSheet, lang: Lang) -> list[str]:
    """Recommendation, then why (when there are reasons): what the summary-mode composer
    writes itself."""
    lb = _LABELS[lang]
    best = sheet.best
    head = lb["best"] if sheet.status == "ok" else lb["least_bad"].format(window=f"{{{{{sheet.window}}}}}")
    lines = [f"**{head}: {{{{{best.venue}}}}} ({when(best)})**"]
    if why := phrases(best.reasons, lang):
        lines.append(f"**{lb['why'] if sheet.status == 'ok' else lb['problem']}:** " + "; ".join(why) + ".")
    return lines


def tail_lines(sheet: FactSheet, lang: Lang) -> list[str]:
    """Everything after recommendation + why: carry, watch for, avoid, options, why not."""
    lb = _LABELS[lang]
    out: list[str] = []
    if sheet.status == "no_safe_window":
        out.append(lb["improves"].format(time=f"{{{{{sheet.improves_at}}}}}") if sheet.improves_at else lb["no_improve"])
    sections: list[tuple[str, list[str]]] = [
        ("carry", phrases(sheet.carry, lang, "carry")),
        ("watch", phrases(sheet.watch, lang, "watch")),
        ("avoid", [p for it in sheet.avoid if (p := avoid_phrase(it, lang))]),
    ]
    out += [f"**{lb[k]}:** " + "; ".join(v) + "." for k, v in sections if v]
    options = []
    for s, where in ((sheet.alt_venue, None), (sheet.alt_time, lb["here"])):
        if s is None:
            continue
        reason = phrases(s.reasons, lang)
        place = where or f"{{{{{s.venue}}}}}"
        options.append(f"{place}, {when(s)}" + (f" — {reason[0]}" if reason else ""))
    if options:
        out.append(f"**{lb['other']}:** " + f"; {lb['or']} ".join(options) + ".")
    if sheet.why_not and (reason := phrases(sheet.why_not.reasons, lang)):
        out.append(f"**{lb['why_not'].format(venue='{{' + sheet.why_not.venue + '}}')}:** {reason[0]}.")
    return out


def facts_block(sheet: FactSheet) -> str:
    """The composer's compact, digit-free facts block (English phrases, refs only)."""
    en = Lang.en
    lines = [f"Activity: {sheet.activity.value}"
             + (" · " + ", ".join(PROFILE_WORDS[en][p] for p in sheet.profile) if sheet.profile else ""),
             f"Requested time: {{{{{sheet.window}}}}}"]  # fmt: skip
    b = sheet.best
    if sheet.status == "ok":
        lines.append(f"Best: {{{{{b.venue}}}}} ({b.kind}), {when(b)}")
        lines += ["Why:"] + [f"- {p}" for p in phrases(b.reasons, en)]
    else:
        lines.append("Status: NO SAFE TIME in the requested window; least-bad option below")
        lines.append(f"Least bad: {{{{{b.venue}}}}} ({b.kind}), {when(b)}")
        lines += ["Problems:"] + [f"- {p}" for p in phrases(b.reasons, en)]
        lines.append(f"Improves from: {{{{{sheet.improves_at}}}}}" if sheet.improves_at
                     else "Improves: not within the following two days")  # fmt: skip
    for title, s in (("Other place", sheet.alt_venue), ("Other time here", sheet.alt_time)):
        if s:
            reason = phrases(s.reasons, en)
            place = f"{{{{{s.venue}}}}} ({s.kind}), " if title == "Other place" else ""
            lines.append(f"{title}: {place}{when(s)}" + (f" — {reason[0]}" if reason else ""))
    for title, kind, items in (("Carry", "carry", sheet.carry), ("Watch for", "watch", sheet.watch)):
        if ps := phrases(items, en, kind):
            lines.append(f"{title}: " + "; ".join(ps))
    if avoid := [p for it in sheet.avoid if (p := avoid_phrase(it, en))]:
        lines.append("Avoid: " + "; ".join(avoid))
    if sheet.why_not and (reason := phrases(sheet.why_not.reasons, en)):
        lines.append(f"Why not {{{{{sheet.why_not.venue}}}}} ({sheet.why_not.kind}): {reason[0]}")
    return "\n".join(lines)

