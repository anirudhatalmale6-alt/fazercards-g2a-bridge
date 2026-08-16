"""Title normalisation.

Supplier and store titles describe the same thing in different words:

    FazerCards : "Xbox Game Pass Essential 6 Months INDIA"
    G2A        : "Xbox Game Pass Essential 6 Months - Xbox Live Key - INDIA"

Comparing those raw gives a poor score.  So we split every title into three
parts and compare them separately:

    core         -- the product itself, with marketing noise removed
    platform     -- steam / xbox / psn / ...
    region       -- GLOBAL / EU / INDIA / ...
    denomination -- (350, "HKD") for gift cards, None otherwise

The denomination is the important one.  A 350 HKD Roblox card and a 100 HKD
Roblox card have almost identical titles; mapping one to the other would sell a
customer the wrong value and cost real money.  It is treated as a hard gate, not
as a similarity signal.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Words that appear in store titles as packaging, not as product identity.
_NOISE_PHRASES = (
    "digital download",
    "digital code",
    "digital key",
    "activation code",
    "activation key",
    "gift card",
    "giftcard",
    "prepaid card",
    "game pass",  # kept in core via _PROTECTED below
    "xbox live key",
    "psn key",
    "steam key",
    "steam gift",
    "cd key",
    "cd-key",
    "product key",
    "key card",
    "voucher",
    "e-code",
    "ecode",
    "official website key",
    "official website",
    "pc download",
)
# Phrases that look like noise but are part of the product's real name.
_PROTECTED = ("game pass", "gift card", "giftcard")

_PLATFORM_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("steam", ("steam",)),
    ("xbox", ("xbox", "xbl", "microsoft store", "windows store")),
    ("psn", ("psn", "playstation", "ps4", "ps5", "ps plus")),
    ("nintendo", ("nintendo", "switch", "eshop")),
    ("epic", ("epic games", "epic")),
    ("origin", ("origin", "ea app", "ea play")),
    ("uplay", ("uplay", "ubisoft connect", "ubisoft")),
    ("rockstar", ("rockstar", "social club")),
    ("battlenet", ("battle.net", "battlenet", "blizzard")),
    ("gog", ("gog",)),
    ("roblox", ("roblox",)),
    ("google", ("google play", "google")),
    ("apple", ("app store", "itunes", "apple")),
    ("amazon", ("amazon",)),
    ("netflix", ("netflix",)),
    ("spotify", ("spotify",)),
)

# Region tokens.  Order matters: longer/more specific first.
_REGION_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("GLOBAL", ("global", "worldwide", "row", "rest of the world")),
    ("EU", ("europe", "european union", "eu")),
    ("EEA", ("eea",)),
    ("NA", ("north america", "na")),
    ("LATAM", ("latam", "latin america")),
    ("MENA", ("mena", "middle east")),
    ("US", ("united states", "usa", "us")),
    ("UK", ("united kingdom", "uk", "gb", "great britain")),
    ("INDIA", ("india", "in")),
    ("BRAZIL", ("brazil", "br")),
    ("TURKEY", ("turkey", "tr", "turkiye")),
    ("ARGENTINA", ("argentina", "ar")),
    ("RUSSIA", ("russia", "ru", "cis")),
    ("HONG KONG", ("hong kong", "hk")),
    ("SINGAPORE", ("singapore", "sg")),
    ("JAPAN", ("japan", "jp")),
    ("GERMANY", ("germany", "de")),
    ("FRANCE", ("france", "fr")),
    ("POLAND", ("poland", "pl")),
    ("CANADA", ("canada", "ca")),
    ("AUSTRALIA", ("australia", "au")),
    ("MEXICO", ("mexico", "mx")),
    ("SPAIN", ("spain", "es")),
    ("ITALY", ("italy", "it")),
    ("NETHERLANDS", ("netherlands", "nl")),
    ("SAUDI ARABIA", ("saudi arabia", "ksa", "sa")),
    ("UAE", ("uae", "united arab emirates")),
    ("SOUTH AFRICA", ("south africa", "za")),
)

_CURRENCY_CODES = {
    "usd", "eur", "gbp", "inr", "hkd", "jpy", "brl", "try", "pln", "cad", "aud",
    "mxn", "ars", "rub", "sgd", "zar", "aed", "sar", "chf", "sek", "nok", "dkk",
    "czk", "huf", "ron", "php", "thb", "idr", "myr", "krw", "twd", "vnd", "nzd",
}
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}

_MONTH_WORDS = ("month", "months", "day", "days", "year", "years")

_ROMAN = {
    " i ": " 1 ", " ii ": " 2 ", " iii ": " 3 ", " iv ": " 4 ", " v ": " 5 ",
    " vi ": " 6 ", " vii ": " 7 ", " viii ": " 8 ", " ix ": " 9 ", " x ": " 10 ",
}


@dataclass(frozen=True)
class TitleParts:
    core: str
    platform: str | None
    region: str | None
    denomination: tuple[float, str | None] | None
    tokens: frozenset[str]

    @property
    def match_key(self) -> str:
        """Coarse bucket used to shortlist candidates before scoring."""
        denom = ""
        if self.denomination is not None:
            amount, currency = self.denomination
            denom = f"|{amount:g}{currency or ''}"
        return f"{self.core}|{self.platform or ''}|{self.region or ''}{denom}"


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def basic_normalize(text: str) -> str:
    """Lowercase, de-accent, drop punctuation, collapse whitespace."""
    lowered = strip_accents(text or "").lower()
    lowered = lowered.replace("&", " and ")
    lowered = re.sub(r"[^a-z0-9%.\s]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def extract_platform(text: str) -> str | None:
    lowered = basic_normalize(text)
    padded = f" {lowered} "
    for platform, needles in _PLATFORM_PATTERNS:
        for needle in needles:
            if f" {needle} " in padded:
                return platform
    return None


def extract_region(text: str) -> str | None:
    """Find the region.

    Two-letter aliases are only honoured at the end of the title (where store
    titles put them), because "in" and "it" are far too common as ordinary words
    to trust anywhere else.
    """
    lowered = basic_normalize(text)
    padded = f" {lowered} "
    tail = " ".join(lowered.split()[-3:])
    for region, aliases in _REGION_ALIASES:
        for alias in aliases:
            if len(alias) <= 2:
                if re.search(rf"\b{re.escape(alias)}$", tail):
                    return region
            elif f" {alias} " in padded:
                return region
    return None


def extract_denomination(text: str) -> tuple[float, str | None] | None:
    """Pull the face value out of a gift-card style title.

    Understands "350 HKD", "HKD 350", "$50", "50$", "€20".  Returns None when
    the title carries no face value (an ordinary game key), and deliberately
    ignores durations like "6 Months" so a subscription length is never mistaken
    for a monetary amount.
    """
    raw = strip_accents(text or "")
    lowered = raw.lower()

    for symbol, currency in _CURRENCY_SYMBOLS.items():
        m = re.search(rf"{re.escape(symbol)}\s*(\d+(?:[.,]\d+)?)", raw)
        if m:
            return (float(m.group(1).replace(",", ".")), currency)
        m = re.search(rf"(\d+(?:[.,]\d+)?)\s*{re.escape(symbol)}", raw)
        if m:
            return (float(m.group(1).replace(",", ".")), currency)

    codes = "|".join(sorted(_CURRENCY_CODES))
    m = re.search(rf"(\d+(?:[.,]\d+)?)\s*({codes})\b", lowered)
    if m:
        return (float(m.group(1).replace(",", ".")), m.group(2).upper())
    m = re.search(rf"\b({codes})\s*(\d+(?:[.,]\d+)?)", lowered)
    if m:
        return (float(m.group(2).replace(",", ".")), m.group(1).upper())

    # A bare number is a face value only when nothing else explains it.
    m = re.search(r"\b(\d{2,6})\b", lowered)
    if m:
        following = lowered[m.end() : m.end() + 12]
        preceding = lowered[max(0, m.start() - 12) : m.start()]
        if any(w in following or w in preceding for w in _MONTH_WORDS):
            return None
        if re.search(r"\b(card|credit|points|coins|robux|wallet|balance|top ?up|gift)\b", lowered):
            return (float(m.group(1)), None)
    return None


def _strip_noise(text: str) -> str:
    result = f" {text} "
    for phrase in _NOISE_PHRASES:
        if phrase in _PROTECTED:
            continue
        result = result.replace(f" {phrase} ", " ")
    for platform, needles in _PLATFORM_PATTERNS:
        for needle in needles:
            result = result.replace(f" {needle} ", " ")
    for _region, aliases in _REGION_ALIASES:
        for alias in aliases:
            if len(alias) > 2:
                result = result.replace(f" {alias} ", " ")
    result = re.sub(r"\s+", " ", result).strip()
    # Trailing 2-letter region codes ("... india in") are packaging too.
    result = re.sub(r"\b(?:key|keys|code|codes)\b", " ", result)
    return re.sub(r"\s+", " ", result).strip()


def parse_title(text: str) -> TitleParts:
    """Split a title into the parts we compare."""
    platform = extract_platform(text)
    region = extract_region(text)
    denomination = extract_denomination(text)

    normalized = basic_normalize(text)
    padded = f" {normalized} "
    for roman, arabic in _ROMAN.items():
        padded = padded.replace(roman, arabic)
    core = _strip_noise(padded.strip())

    # Drop the region's own two-letter tail if it survived the pass above.
    core = re.sub(r"\b[a-z]{2}$", "", core).strip() if region and len(core) > 3 else core
    tokens = frozenset(t for t in core.split() if len(t) > 1 or t.isdigit())
    return TitleParts(
        core=core,
        platform=platform,
        region=region,
        denomination=denomination,
        tokens=tokens,
    )


def parse_title_with(
    text: str, *, region: str | None = None, platform: str | None = None
) -> TitleParts:
    """Parse a title but prefer the structured values the API gave us.

    Both APIs return region and platform as their own fields.  Those are far more
    reliable than anything guessed from the title, and using them turns a pile of
    "unknown, so half credit" scores into exact agreement -- which is the
    difference between a product landing in the review queue and being mapped
    automatically.
    """
    parts = parse_title(text)
    resolved_region = (region or "").strip().upper() or parts.region
    if resolved_region:
        # Normalise supplier spellings ("Hong Kong", "hk") onto our own codes.
        canonical = extract_region(resolved_region) or resolved_region
        resolved_region = canonical
    resolved_platform = extract_platform(platform or "") or parts.platform
    return TitleParts(
        core=parts.core,
        platform=resolved_platform,
        region=resolved_region,
        denomination=parts.denomination,
        tokens=parts.tokens,
    )
