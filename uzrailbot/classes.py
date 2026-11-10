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


def family(code: str) -> str:
    """The kind of car behind a class code: 3 = plaskart, 2 = kupe, 1 = SV/lux; the letters С, В, Е = sitting."""
    code = str(code or "")
    if not is_class_code(code):
        return ""
    return "sitting" if code[1] in "СВЕ" else {"3": "plaskart", "2": "kupe", "1": "sv"}[code[0]]


def label(code: str, lang: str = "en") -> str:
    c = CLASSES.get(code)
    if not c:
        fam = {"sitting": "💺 Sitting", "plaskart": "🛏 Plaskart", "kupe": "🚪 Coupe", "sv": "👑 SV"}.get(family(code))
        return f"{fam} ({code})" if fam else code
    return f"{c['emoji']} {c.get(lang) or c['en']}"


# ---------------------------------------------------------------- where berths are in a sleeping car
BERTHS = {"plaskart": 6, "kupe": 4, "sv": 2}      # berths in one bay / compartment


def seat_block(code: str, seat: int):
    """The compartment (kupe, SV) or bay (plaskart) a berth is in, counted from 0. None for sitting cars and for
    seat numbers a standard car doesn't have.
    Kupe: 4 berths per compartment, 1-4, 5-8, ... SV and Lux: 2 per compartment. Plaskart: a bay is 4 berths
    (1-4, 5-8, ... 33-36) plus the 2 side berths across the aisle, which are numbered from the far end of the car
    (bay 1 has 53-54, bay 9 has 37-38)."""
    fam, seat = family(code), int(seat)
    if fam == "kupe":
        return (seat - 1) // 4 if 1 <= seat <= 40 else None
    if fam == "sv":
        return (seat - 1) // 2 if 1 <= seat <= 20 else None
    if fam == "plaskart":
        if 1 <= seat <= 36:
            return (seat - 1) // 4
        return (54 - seat) // 2 if 37 <= seat <= 54 else None
    return None


def blocks_used(code: str, seats: list) -> int:
    """How many compartments or bays these seats are spread over (1 when the car has none)."""
    return len({seat_block(code, s) for s in seats})


def sits_together(code: str, seats: list) -> bool:
    """True when these seats are one compartment or bay, or neighbouring ones when the party is bigger than one
    holds. A car without compartments (sitting) always counts: the same car is all we can tell."""
    blocks = {seat_block(code, s) for s in seats}
    if blocks == {None}:
        return True
    if None in blocks:
        return False
    need = -(-len(seats) // BERTHS[family(code)])
    return len(blocks) <= need and max(blocks) - min(blocks) < need
