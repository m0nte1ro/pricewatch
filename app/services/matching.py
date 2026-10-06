import logging
import re
import unicodedata
from difflib import SequenceMatcher

from app.schemas.domain import Candidate, Condition, Identity, Match

log = logging.getLogger(__name__)
BRANDS = (
    "TCL",
    "SAMSUNG",
    "LG",
    "SONY",
    "PHILIPS",
    "HISENSE",
    "PANASONIC",
    "XIAOMI",
    "APPLE",
    "BOSCH",
    "ASUS",
    "LENOVO",
    "DELL",
    "NINTENDO",
    "DYSON",
)
NOISE = {
    "MINILED",
    "QLED",
    "OLED",
    "4K",
    "8K",
    "UHD",
    "HDR10",
    "HDMI2",
    "WIFI6",
    "120HZ",
    "144HZ",
    "216CM",
}


def normalize(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKD", value or "").upper())


def identify(
    title: str,
    *,
    brand: str | None = None,
    model: str | None = None,
    category: str = "general",
    identifiers: dict | None = None,
) -> Identity:
    upper = title.upper()
    brand = brand or next((b for b in BRANDS if re.search(rf"\b{b}\b", upper)), None)
    if brand and brand.upper() in BRANDS:
        brand = brand.upper()
    # Size separated from a TV model, e.g. '85 C7K', has the same identity as '85C7K'.
    compact = re.sub(r"\b(\d{2,3})\s+([A-Z]\d[A-Z0-9]*)\b", r"\1\2", upper)
    if not model:
        tokens = re.findall(r"\b[A-Z0-9][A-Z0-9.-]{2,}\b", compact)
        model = next(
            (
                t
                for t in tokens
                if any(c.isalpha() for c in t)
                and any(c.isdigit() for c in t)
                and normalize(t) not in NOISE
                and not re.fullmatch(r"\d+(?:HZ|CM|GB|TB|W|P)", t)
            ),
            None,
        )
        if model:
            suffix = re.search(rf"\b{re.escape(model)}\s+(PRO|MAX|PLUS|ULTRA)\b", compact)
            if suffix:
                model += suffix.group(1)
    model = normalize(model) or None
    size_match = re.search(r'(\d{2,3})\s*(?:["″]|POLEGADAS|INCH)', upper)
    model_size = re.search(r"^(?:OLED|QE|UE|TQ|GQ|KD|XR|K)?(\d{2,3})[A-Z]", model or "")
    is_tv = bool(
        re.search(r"\b(?:TV|TELEVISOR|TELEVISAO|TELEVISION|QLED|MINILED|OLED|BRAVIA)\b", upper)
    )
    if category == "tv" or is_tv or (brand in BRANDS[:9] and model_size):
        category = "tv"
    size = (
        (size_match or model_size).group(1)
        if category == "tv" and (size_match or model_size)
        else None
    )
    canonical = " ".join(filter(None, [brand, model])) if model else title.strip()
    return Identity(
        name=canonical,
        brand=brand,
        model=model,
        size=size,
        category=category,
        identifiers=identifiers or {},
    )


def family_code(short: str, full: str, size: str | None) -> bool:
    """True when a family model (QN90D) is embedded in a regional code (TQ65QN90DATXXC)."""
    if not short[:1].isalpha() or len(short) < 4:
        return False
    match = re.fullmatch(rf"[A-Z]{{0,4}}(\d{{2,3}}){re.escape(short)}[A-Z0-9]*", full)
    return bool(match and (not size or match.group(1) == normalize(size)))


def match_identity(
    expected: Identity, actual: Identity, condition: Condition = Condition.UNKNOWN
) -> Match:
    for field in ("brand", "size", "model"):
        a, b = getattr(expected, field), getattr(actual, field)
        if a and b and normalize(a) != normalize(b):
            if field == "model":
                short, full = sorted((normalize(a), normalize(b)), key=len)
                if family_code(short, full, expected.size or actual.size):
                    return Match(
                        level="LOW",
                        score=0.6,
                        reason=f"Model {short} appears in manufacturer code {full}; confirm",
                    )
            return Match(level="CONFLICT", score=0, reason=f"Different {field}: {a} / {b}")
    shared = expected.identifiers.keys() & actual.identifiers.keys()
    same_model = bool(
        expected.model and actual.model and normalize(expected.model) == normalize(actual.model)
    )
    if any(expected.identifiers[k] != actual.identifiers[k] for k in shared):
        if not same_model:
            return Match(level="CONFLICT", score=0, reason="Manufacturer identifiers differ")
        # Retailers relabel outlet/refurbished stock with their own barcode (e.g. Worten Outlet).
        if condition not in (Condition.NEW, Condition.UNKNOWN):
            return Match(
                level="HIGH",
                score=0.9,
                reason="Exact model; retailer barcode on outlet/second-hand stock",
            )
        return Match(
            level="LOW", score=0.6, reason="Exact model but different barcode; confirm identity"
        )
    if same_model:
        return Match(level="EXACT", score=1, reason="Exact model match; no conflicting attributes")
    if shared:
        return Match(level="HIGH", score=0.98, reason="Manufacturer identifier match")
    score = SequenceMatcher(None, normalize(expected.name), normalize(actual.name)).ratio()
    return Match(
        level="LOW", score=min(score, 0.69), reason="Title similarity only; confirm identity"
    )


def same_listing(a: Candidate, b: Candidate) -> bool:
    x, y = a.listing, b.listing
    if (x.retailer, x.condition, normalize(x.seller)) != (
        y.retailer,
        y.condition,
        normalize(y.seller),
    ):
        return False
    if x.retailer_product_id and y.retailer_product_id:
        if x.retailer_product_id == y.retailer_product_id:
            return True
        return x.url == y.url
    if x.url == y.url:
        return True
    # Do not collapse distinct offers when both have authoritative IDs.
    return (
        normalize(x.title) == normalize(y.title)
        and match_identity(x.identity, y.identity).level == "EXACT"
    )


def deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    merged: list[Candidate] = []
    for candidate in candidates:
        existing = next((c for c in merged if same_listing(c, candidate)), None)
        if existing:
            existing.sources = sorted(set(existing.sources + candidate.sources))
            log.info("listing_deduplicated", extra={"retailer": candidate.listing.retailer})
        else:
            merged.append(candidate)
    return merged
