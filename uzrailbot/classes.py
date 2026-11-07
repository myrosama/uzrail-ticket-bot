"""Ticket classes. The railway's car type NAME changes with the site language, the class CODE does not,
so everything matches on the code (Cyrillic letters, exactly as the API returns them)."""

CLASSES = {
    "3П": {"en": "Plaskart (open sleeper)", "uz": "Plaskart", "ru": "Плацкарт", "emoji": "🛏"},
    "2К": {"en": "Coupe (4 berths)", "uz": "Kupe", "ru": "Купе", "emoji": "🚪"},
    "1Л": {"en": "SV / Lux (2 berths)", "uz": "SV", "ru": "СВ", "emoji": "👑"},
    "1С": {"en": "Sitting, business / 1st", "uz": "O'rindiqli, biznes", "ru": "Сидячий, бизнес", "emoji": "💺"},
    "2Е": {"en": "Sitting, economy", "uz": "O'rindiqli, ekonom", "ru": "Сидячий, эконом", "emoji": "🪑"},
    "1В": {"en": "Sitting, 1st class", "uz": "O'rindiqli, 1-klass", "ru": "Сидячий, 1 класс", "emoji": "💺"},
    "2В": {"en": "Sitting, 2nd class", "uz": "O'rindiqli, 2-klass", "ru": "Сидячий, 2 класс", "emoji": "🪑"},
}


def label(code: str, lang: str = "en") -> str:
    c = CLASSES.get(code)
    if not c:
        return code
    return f"{c['emoji']} {c.get(lang) or c['en']}"
