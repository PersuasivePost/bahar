"""Deterministic user-facing text in the user's language. Step 6: the gate messages
(out of scope, and the one clarifying question: activity, location or commute end)."""

from typing import Final

from bahar.models import Lang

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
