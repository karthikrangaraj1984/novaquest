"""Languages for students (1.22.0).

A student chooses their language on Me. The app's own words on the main student screens (menus, buttons, Home,
Tasks, assignments, Daily 5) come from the tables below: Hindi and Tamil are written in full; the other languages
show those words in English. Everything NOVA says (Ask NOVA, hints, Teach NOVA) and, on request, each question is
translated by Claude into any of the languages, with the English science word kept in brackets. Without Claude,
NOVA stays in English."""
from __future__ import annotations

from typing import Callable

LANGS: dict[str, tuple[str, str]] = {  # code: (name in its own script, English name)
    "en": ("English", "English"), "hi": ("हिन्दी", "Hindi"), "ta": ("தமிழ்", "Tamil"), "te": ("తెలుగు", "Telugu"),
    "kn": ("ಕನ್ನಡ", "Kannada"), "ml": ("മലയാളം", "Malayalam"), "mr": ("मराठी", "Marathi"), "bn": ("বাংলা", "Bengali"),
    "gu": ("ગુજરાતી", "Gujarati"),
}
FULL_UI = ("en", "hi", "ta")

_HI = {
    "Home": "होम", "Learn": "सीखें", "Tasks": "काम", "Me": "मैं",
    "Sign out": "साइन आउट", "Using a shared phone or tablet? Always sign out when you finish.":
        "साझा फ़ोन या टैबलेट है? काम खत्म होने पर हमेशा साइन आउट करें।",
    "Ask NOVA": "NOVA से पूछें", "Answers come only from your textbook, with the page number.":
        "जवाब सिर्फ़ आपकी पाठ्यपुस्तक से आते हैं, पेज नंबर के साथ।",
    "Your question": "आपका सवाल", "Close": "बंद करें", "Ask": "पूछें",
    "Hi {name}!": "नमस्ते {name}!", "Up next": "अगला", "closes in {left}": "{left} में बंद होगा",
    "Continue": "जारी रखें", "Start": "शुरू करें", "All caught up!": "सब हो गया!",
    "Nothing due, and every topic is finished. Revise anything from Learn.":
        "अभी कुछ बाकी नहीं है। ‘सीखें’ से कुछ भी दोहराएँ।",
    "Daily 5": "रोज़ के 5", "Done for today ✓": "आज का हो गया ✓", "5 quick questions · about 3 minutes": "5 छोटे सवाल · लगभग 3 मिनट",
    "In progress": "चल रहा है", "To do": "करना है", "Also for you": "आपके लिए और", "See all {n} tasks ›": "सभी {n} काम देखें ›",
    "{n} to do": "{n} करने हैं", "{n} started": "{n} शुरू",
    "My tasks": "मेरे काम", "All": "सब", "Not started": "शुरू नहीं किया", "Handed in": "जमा किया", "Results ready": "नतीजे तैयार",
    "Coming up": "आने वाले", "Closed": "बंद", "Scorecard": "स्कोरकार्ड", "Soon": "जल्द", "Waiting for teacher": "शिक्षक की प्रतीक्षा",
    "No tasks yet. Your teacher will add them here.": "अभी कोई काम नहीं। आपके शिक्षक यहाँ जोड़ेंगे।",
    "Answers save by themselves. Stuck? Tap “Need a hint?”: the tutor guides you but never gives the answer.":
        "जवाब अपने-आप सेव होते हैं। अटक गए? “संकेत चाहिए?” दबाएँ: ट्यूटर रास्ता दिखाता है, जवाब नहीं बताता।",
    "Question {n} of {m}": "सवाल {n} / {m}", "Need a hint?": "संकेत चाहिए?", "Back": "पीछे", "Next": "आगे",
    "Check and hand in": "जाँचें और जमा करें", "Hand in": "जमा करें",
    "Tap a question to go back to it. You can't change answers after handing in.":
        "किसी सवाल पर वापस जाने के लिए उसे दबाएँ। जमा करने के बाद जवाब बदल नहीं सकते।",
    "Hand in now? You can't change answers afterwards.": "अभी जमा करें? बाद में जवाब नहीं बदल सकते।",
    "Write your answer in your own words": "अपने शब्दों में जवाब लिखें", "Your answer": "आपका जवाब",
    "Choose one answer": "एक जवाब चुनें", "Time left: {left}": "बचा समय: {left}", "All answers saved": "सभी जवाब सेव हैं",
    "Read in {lang}": "{lang} में पढ़ें", "Show in English": "अंग्रेज़ी में दिखाएँ",
    "Work it out on paper, then type the answer. Fractions (3/4), decimals and units all work.":
        "कागज़ पर हल करें, फिर जवाब लिखें। भिन्न (3/4), दशमलव और इकाइयाँ सब चलती हैं।",
    "Five quick questions from topics you've learnt, timed so you remember them for good. Just practice: no marks.":
        "सीखे हुए विषयों से पाँच छोटे सवाल, ताकि आप उन्हें हमेशा याद रखें। सिर्फ़ अभ्यास: कोई अंक नहीं।",
    "All done?": "सब हो गया?", "Finish Daily 5": "रोज़ के 5 पूरे करें", "{n} of {m} right": "{m} में से {n} सही",
    "Back to Home": "होम पर वापस",
    "Step {n} of {m}: {name}": "चरण {n} / {m}: {name}", "Step {n}: {name}": "चरण {n}: {name}",
    "Learn in pictures": "चित्रों से सीखें", "Practise": "अभ्यास", "Prove it": "साबित करें", "Teach NOVA": "NOVA को सिखाएँ",
    "Read aloud": "पढ़कर सुनाएँ", "Talk with NOVA": "NOVA से बात करें",
    "My progress": "मेरी प्रगति", "Language": "भाषा", "Save": "सेव करें",
    "NovaQuest's words on your main screens are shown in your language. NOVA replies in it too, and you can read each question in it.":
        "मुख्य स्क्रीन के शब्द आपकी भाषा में दिखते हैं। NOVA भी इसी भाषा में जवाब देता है, और आप हर सवाल इसमें पढ़ सकते हैं।",
}

_TA = {
    "Home": "முகப்பு", "Learn": "கற்க", "Tasks": "பணிகள்", "Me": "நான்",
    "Sign out": "வெளியேறு", "Using a shared phone or tablet? Always sign out when you finish.":
        "பகிர்ந்த கைபேசி அல்லது டேப்லெட்டா? முடித்ததும் எப்போதும் வெளியேறுங்கள்.",
    "Ask NOVA": "NOVA-விடம் கேள்", "Answers come only from your textbook, with the page number.":
        "பதில்கள் உங்கள் பாடநூலில் இருந்து மட்டுமே, பக்க எண்ணுடன் வரும்.",
    "Your question": "உங்கள் கேள்வி", "Close": "மூடு", "Ask": "கேள்",
    "Hi {name}!": "வணக்கம் {name}!", "Up next": "அடுத்தது", "closes in {left}": "{left}-ல் மூடப்படும்",
    "Continue": "தொடர்க", "Start": "தொடங்கு", "All caught up!": "எல்லாம் முடிந்தது!",
    "Nothing due, and every topic is finished. Revise anything from Learn.":
        "இப்போது எதுவும் நிலுவையில் இல்லை. ‘கற்க’ பகுதியில் எதையும் மீண்டும் படியுங்கள்.",
    "Daily 5": "தினசரி 5", "Done for today ✓": "இன்றைக்கு முடிந்தது ✓", "5 quick questions · about 3 minutes": "5 சிறு கேள்விகள் · சுமார் 3 நிமிடம்",
    "In progress": "நடந்து கொண்டிருக்கிறது", "To do": "செய்ய வேண்டியவை", "Also for you": "உங்களுக்காக மேலும்",
    "See all {n} tasks ›": "அனைத்து {n} பணிகளையும் பார் ›", "{n} to do": "{n} செய்ய வேண்டும்", "{n} started": "{n} தொடங்கியது",
    "My tasks": "என் பணிகள்", "All": "அனைத்தும்", "Not started": "தொடங்கவில்லை", "Handed in": "சமர்ப்பிக்கப்பட்டது",
    "Results ready": "முடிவுகள் தயார்", "Coming up": "வரவிருப்பவை", "Closed": "மூடப்பட்டது", "Scorecard": "மதிப்பெண் அட்டை",
    "Soon": "விரைவில்", "Waiting for teacher": "ஆசிரியருக்காகக் காத்திருக்கிறது",
    "No tasks yet. Your teacher will add them here.": "இன்னும் பணிகள் இல்லை. உங்கள் ஆசிரியர் இங்கே சேர்ப்பார்.",
    "Answers save by themselves. Stuck? Tap “Need a hint?”: the tutor guides you but never gives the answer.":
        "பதில்கள் தானாகச் சேமிக்கப்படும். சிக்கலா? “குறிப்பு வேண்டுமா?” அழுத்துங்கள்: ஆசிரியர் வழிகாட்டுவார், பதிலைச் சொல்ல மாட்டார்.",
    "Question {n} of {m}": "கேள்வி {n} / {m}", "Need a hint?": "குறிப்பு வேண்டுமா?", "Back": "பின்", "Next": "அடுத்து",
    "Check and hand in": "சரிபார்த்துச் சமர்ப்பி", "Hand in": "சமர்ப்பி",
    "Tap a question to go back to it. You can't change answers after handing in.":
        "ஒரு கேள்விக்குத் திரும்ப அதை அழுத்துங்கள். சமர்ப்பித்த பின் பதில்களை மாற்ற முடியாது.",
    "Hand in now? You can't change answers afterwards.": "இப்போது சமர்ப்பிக்கவா? பிறகு பதில்களை மாற்ற முடியாது.",
    "Write your answer in your own words": "உங்கள் சொந்த வார்த்தைகளில் பதில் எழுதுங்கள்", "Your answer": "உங்கள் பதில்",
    "Choose one answer": "ஒரு பதிலைத் தேர்ந்தெடுங்கள்", "Time left: {left}": "மீதமுள்ள நேரம்: {left}",
    "All answers saved": "அனைத்து பதில்களும் சேமிக்கப்பட்டன",
    "Read in {lang}": "{lang}-ல் படி", "Show in English": "ஆங்கிலத்தில் காட்டு",
    "Work it out on paper, then type the answer. Fractions (3/4), decimals and units all work.":
        "தாளில் கணக்கிட்டு, பிறகு பதிலை எழுதுங்கள். பின்னங்கள் (3/4), தசமங்கள், அலகுகள் எல்லாம் ஏற்கப்படும்.",
    "Five quick questions from topics you've learnt, timed so you remember them for good. Just practice: no marks.":
        "நீங்கள் கற்ற பாடங்களிலிருந்து ஐந்து சிறு கேள்விகள், என்றும் நினைவில் நிற்க. வெறும் பயிற்சி: மதிப்பெண் இல்லை.",
    "All done?": "எல்லாம் முடிந்ததா?", "Finish Daily 5": "தினசரி 5-ஐ முடி", "{n} of {m} right": "{m}-ல் {n} சரி",
    "Back to Home": "முகப்புக்குத் திரும்பு",
    "Step {n} of {m}: {name}": "படி {n} / {m}: {name}", "Step {n}: {name}": "படி {n}: {name}",
    "Learn in pictures": "படங்களில் கற்க", "Practise": "பயிற்சி", "Prove it": "நிரூபி", "Teach NOVA": "NOVA-வுக்குக் கற்றுக்கொடு",
    "Read aloud": "சத்தமாகப் படி", "Talk with NOVA": "NOVA-வுடன் பேசு",
    "My progress": "என் முன்னேற்றம்", "Language": "மொழி", "Save": "சேமி",
    "NovaQuest's words on your main screens are shown in your language. NOVA replies in it too, and you can read each question in it.":
        "முக்கிய திரைகளில் உள்ள சொற்கள் உங்கள் மொழியில் தெரியும். NOVA-வும் அதே மொழியில் பதில் தரும்; ஒவ்வொரு கேள்வியையும் அதில் படிக்கலாம்.",
}

STRINGS: dict[str, dict[str, str]] = {"hi": _HI, "ta": _TA}


def name(code: str) -> str:
    return LANGS.get(code, LANGS["en"])[0]


def english_name(code: str) -> str:
    return LANGS.get(code, LANGS["en"])[1]


def translator(lang: str) -> Callable[..., str]:
    """t("Question {n} of {m}", n=1, m=5): the words in the student's language (English if we don't have them)."""
    table = STRINGS.get(lang, {})

    def t(text: str, **kw: object) -> str:
        out = table.get(text, text)
        return out.format(**kw) if kw else out
    return t
