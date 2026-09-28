"""Bounded, category-balanced sampling of the published question bank."""

import random

MAX_TEST_QUESTIONS = 20
MAX_QUESTIONS_PER_CATEGORY = 5
CATEGORY_LIMITS = {"prices": 3}


def test_question_limit(project_limit=None, default_limit=None):
    configured = project_limit or default_limit or MAX_TEST_QUESTIONS
    return max(1, min(MAX_TEST_QUESTIONS, int(configured)))


def allowed_category_counts(counts, distribution=None):
    """Apply the per-topic ceiling as well as any explicit project quota."""
    if distribution:
        return {
            category: min(
                counts.get(category, 0),
                max(0, int(quota)),
                CATEGORY_LIMITS.get(category, MAX_QUESTIONS_PER_CATEGORY),
            )
            for category, quota in distribution.items()
        }
    return {
        category: min(count, CATEGORY_LIMITS.get(category, MAX_QUESTIONS_PER_CATEGORY))
        for category, count in counts.items()
    }


def balanced_sample(buckets, limit=MAX_TEST_QUESTIONS, distribution=None, rng=None, shuffle=True):
    """Select across categories and fact families without overfilling a topic.

    Bucket keys may be a category or ``(category, family)``. Sampling rotates
    among families (for example, among school/metro/park transport facts), then
    among categories. A project with too few eligible categories produces a
    shorter quiz instead of allowing a single subject to exceed the hard cap.
    """
    rng = rng or random.SystemRandom()
    grouped = {}
    for key, items in buckets.items():
        category, family = key if isinstance(key, tuple) else (key, "default")
        grouped.setdefault(category, {})[family] = list(items)
    counts = allowed_category_counts(
        {category: sum(map(len, families.values())) for category, families in grouped.items()},
        distribution,
    )
    pools = {}
    for category, count in counts.items():
        if count:
            families = {}
            for family, source_items in grouped[category].items():
                items = list(source_items)
                if shuffle:
                    rng.shuffle(items)
                if items:
                    families[family] = items
            family_names = list(families)
            if shuffle:
                rng.shuffle(family_names)
            selected = []
            while family_names and len(selected) < count:
                remaining = []
                for family in family_names:
                    selected.append(families[family].pop())
                    if families[family]:
                        remaining.append(family)
                    if len(selected) == count:
                        break
                family_names = remaining
            pools[category] = selected
    categories = list(pools)
    if shuffle:
        rng.shuffle(categories)
    selected = []
    positions = {category: 0 for category in categories}
    limit = min(MAX_TEST_QUESTIONS, max(0, int(limit)))
    while categories and len(selected) < limit:
        remaining = []
        for category in categories:
            index = positions[category]
            selected.append(pools[category][index])
            positions[category] = index + 1
            if index + 1 < len(pools[category]):
                remaining.append(category)
            if len(selected) == limit:
                break
        categories = remaining
    if shuffle:
        rng.shuffle(selected)
    return selected
