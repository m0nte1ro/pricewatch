import pytest

from app.schemas.domain import Condition, Identity
from app.services.matching import deduplicate, identify, match_identity


@pytest.mark.parametrize(
    "title",
    [
        "TCL 85C7K",
        "TCL 85 C7K",
        'TCL TV MiniLED 85C7K 85"',
        "Televisor TCL 85C7K Mini LED 85 polegadas",
    ],
)
def test_tv_normalization(title):
    identity = identify(title)
    assert (identity.brand, identity.model, identity.size, identity.category) == (
        "TCL",
        "85C7K",
        "85",
        "tv",
    )


@pytest.mark.parametrize("other", ["TCL 85C8K", "TCL 75C7K", "SAMSUNG 85C7K", "TCL 85C7K PRO"])
def test_never_merge_conflicting_models(other):
    assert match_identity(identify("TCL 85C7K"), identify(other)).level == "CONFLICT"


def test_title_only_requires_review():
    assert (
        match_identity(identify("A beautiful television"), identify("A beautiful television")).level
        == "LOW"
    )


def test_identifier_match_and_conflict():
    a = Identity(name="Monitor", identifiers={"gtin13": "123"})
    b = Identity(name="A monitor", identifiers={"gtin13": "123"})
    assert match_identity(a, b).level == "HIGH"
    b.identifiers = {"gtin13": "456"}
    assert match_identity(a, b).level == "CONFLICT"


def test_additive_deduplication(candidate):
    auto = candidate.model_copy(deep=True)
    auto.sources = ["discovered"]
    auto.listing.url += "?irrelevant=1"
    result = deduplicate([candidate, auto])
    assert len(result) == 1
    assert result[0].sources == ["discovered", "manual"]


@pytest.mark.parametrize(
    "field,value",
    [("condition", "outlet_grade_a"), ("seller", "Other store"), ("retailer_product_id", "999")],
)
def test_separate_offers_remain_separate(candidate, field, value):
    other = candidate.model_copy(deep=True)
    setattr(other.listing, field, value)
    if field == "retailer_product_id":
        other.listing.url += "?offer=other"
    assert len(deduplicate([candidate, other])) == 2


def test_title_model_fallback_without_ids(candidate):
    other = candidate.model_copy(deep=True)
    candidate.listing.retailer_product_id = None
    other.listing.retailer_product_id = None
    other.listing.url += "?variant=1"
    assert len(deduplicate([candidate, other])) == 1


def test_same_canonical_url_with_changed_id_does_not_duplicate(candidate):
    other = candidate.model_copy(deep=True)
    other.listing.retailer_product_id = "changed"
    assert len(deduplicate([candidate, other])) == 1


def test_regional_manufacturer_code_requires_review():
    family = identify('Samsung TV Neo QLED 65" QN90D')
    regional = identify("TV SAMSUNG TQ65QN90DATXXC 65 Neo QLED")
    assert regional.size == "65"
    match = match_identity(family, regional)
    assert match.level == "LOW" and "TQ65QN90DATXXC" in match.reason


@pytest.mark.parametrize(
    "other",
    ["TV SAMSUNG TQ55QN90DATXXC 55 Neo QLED", "TV SAMSUNG TQ65QN95DATXXC 65", "LG 65QN90D"],
)
def test_regional_code_rule_does_not_merge_other_models(other):
    family = identify('Samsung TV Neo QLED 65" QN90D')
    assert match_identity(family, identify(other)).level == "CONFLICT"


def test_sony_model_size():
    identity = identify("Sony Bravia 7 K-65XR70")
    assert (identity.model, identity.size, identity.category) == ("K65XR70", "65", "tv")


def test_outlet_relabelled_with_retailer_barcode_matches():
    new = identify("TCL 85C7K", identifiers={"gtin13": "5901292525712"})
    outlet = identify("TCL 85C7K Outlet Grade A", identifiers={"gtin13": "5608947517718"})
    assert match_identity(new, outlet, Condition.A).level == "HIGH"
    # A new item with the same model but another barcode is plausible but must be reviewed.
    assert match_identity(new, outlet, Condition.NEW).level == "LOW"
    # A different barcode never rescues a different model.
    other = identify("TCL 85C8K", identifiers={"gtin13": "5608947517718"})
    assert match_identity(new, other, Condition.A).level == "CONFLICT"
