import random
from collections import Counter

from glorax.selection import balanced_sample
from glorax.selection import test_question_limit as effective_limit


def pools(sizes):
    return {category: [(category, i) for i in range(size)] for category, size in sizes.items()}


def test_large_transport_bank_does_not_crowd_out_other_topics():
    bank = pools({"transport": 80, "prices": 30, "layouts": 25, "location": 20, "buildings": 20})
    for seed in range(30):
        result = balanced_sample(bank, rng=random.Random(seed))
        assert len(result) == len(set(result)) == 20
        counts = Counter(category for category, _ in result)
        assert counts["prices"] == 3
        assert max(counts.values()) <= 5
        assert set(counts) == set(bank)


def test_scarce_categories_are_included_but_no_category_exceeds_five():
    bank = pools({"transport": 80, "prices": 1, "layouts": 2, "location": 3})
    result = balanced_sample(bank, rng=random.Random(8))
    assert Counter(category for category, _ in result) == {
        "transport": 5,
        "prices": 1,
        "layouts": 2,
        "location": 3,
    }
    assert len(result) == len(set(result)) == 11
    assert sum(len(values) for values in bank.values()) == 86  # Input bank remains intact.


def test_small_bank_no_padding_and_single_category():
    assert len(balanced_sample(pools({"location": 2, "prices": 3}))) == 5
    assert len(balanced_sample(pools({"transport": 80}))) == 5
    assert balanced_sample({}) == []


def test_family_sampling_spreads_transport_across_map_categories():
    bank = {
        ("transport", "school"): [("transport", "school", i) for i in range(50)],
        ("transport", "kindergarten"): [("transport", "kindergarten", i) for i in range(30)],
        ("transport", "metro"): [("transport", "metro", i) for i in range(8)],
        ("prices", "flat"): [("prices", "flat", i) for i in range(8)],
        ("layouts", "area"): [("layouts", "area", i) for i in range(8)],
        ("buildings", "floors"): [("buildings", "floors", i) for i in range(8)],
    }
    result = balanced_sample(bank, rng=random.Random(11))
    counts = Counter(row[0] for row in result)
    transport_families = Counter(row[1] for row in result if row[0] == "transport")
    assert len(result) == 18
    assert counts == {"transport": 5, "prices": 3, "layouts": 5, "buildings": 5}
    assert set(transport_families) == {"school", "kindergarten", "metro"}
    assert sum(transport_families.values()) == 5
    assert max(transport_families.values()) - min(transport_families.values()) <= 1


def test_sampling_and_order_vary_between_attempts():
    bank = pools({"transport": 80, "prices": 80, "layouts": 80})
    first = balanced_sample(bank, rng=random.Random(1))
    second = balanced_sample(bank, rng=random.Random(2))
    assert set(first) != set(second)
    assert first != second
    counts = Counter(category for category, _ in first)
    assert counts == {"transport": 5, "prices": 3, "layouts": 5}


def test_more_categories_than_seats_randomizes_category_ties():
    bank = pools({str(i): 3 for i in range(25)})
    results = [balanced_sample(bank, rng=random.Random(seed)) for seed in (1, 2)]
    assert all(len(set(category for category, _ in result)) == 20 for result in results)
    assert {category for category, _ in results[0]} != {category for category, _ in results[1]}


def test_explicit_admin_quotas_and_limits_are_respected():
    bank = pools({"transport": 80, "prices": 5, "layouts": 10})
    result = balanced_sample(bank, limit=9, distribution={"prices": 2, "layouts": 5}, shuffle=False)
    assert Counter(category for category, _ in result) == {"prices": 2, "layouts": 5}
    assert (
        effective_limit(None, None) == effective_limit(80, 100) == effective_limit(None, 80) == 20
    )
    assert effective_limit(5, 10) == 5
    assert effective_limit(None, 12) == 12
    assert len(balanced_sample(bank, limit=1000)) == 13


def test_price_cap_applies_even_to_explicit_admin_quotas():
    bank = pools({"prices": 80, "layouts": 80})
    result = balanced_sample(bank, distribution={"prices": 20, "layouts": 20})
    assert Counter(category for category, _ in result) == {"prices": 3, "layouts": 5}
