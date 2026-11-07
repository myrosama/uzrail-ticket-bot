"""Ticket classes. The railway's car type NAME changes with the site language, the class CODE does not,
so everything matches on the code (Cyrillic letters, exactly as the API returns them)."""

CLASSES = {
    "3П": {"en": "Plaskart (open sleeper)", "uz": "Plaskart", "ru": "Плацкарт", "emoji": "🛏"},
    "2К": {"en": "Coupe (4 berths)", "uz": "Kupe", "ru": "Купе", "emoji": "🚪"},
    "1Л": {"en": "SV (2 berths)", "uz": "SV", "ru": "СВ", "emoji": "👑"},
    "1А": {"en": "Lux (premium 2-berth)", "uz": "Lyuks", "ru": "Люкс", "emoji": "💎"},
    "1С": {"en": "Sitting, business / 1st", "uz": "O'rindiqli, biznes", "ru": "Сидячий, бизнес", "emoji": "💺"},
    "2Е": {"en": "Sitting, economy", "uz": "O'rindiqli, ekonom", "ru": "Сидячий, эконом", "emoji": "🪑"},
    "1В": {"en": "Sitting, 1st class", "uz": "O'rindiqli, 1-klass", "ru": "Сидячий, 1 класс", "emoji": "💺"},
    "2В": {"en": "Sitting, 2nd class", "uz": "O'rindiqli, 2-klass", "ru": "Сидячий, 2 класс", "emoji": "🪑"},
}


def is_class_code(code: str) -> bool:
    """Known codes, plus any other railway service class code (digit + Cyrillic letter, e.g. 3Л, 2У)."""
    code = str(code or "")
    return code in CLASSES or (len(code) == 2 and code[0] in "123" and "А" <= code[1].upper() <= "Я")


def label(code: str, lang: str = "en") -> str:
    c = CLASSES.get(code)
    if not c:
        # unknown variants: name the family (3 = plaskart, 2 = kupe, 1 = SV/lux; С/В/Е = sitting)
        if not is_class_code(code):
            return code
        fam = ("💺 Sitting" if code[1] in "СВЕ" else {"3": "🛏 Plaskart", "2": "🚪 Coupe", "1": "👑 SV"}[code[0]])
        return f"{fam} ({code})"
    return f"{c['emoji']} {c.get(lang) or c['en']}"
