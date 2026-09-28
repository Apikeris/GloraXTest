"""Bounded, category-balanced sampling of the published question bank."""
import random

MAX_TEST_QUESTIONS = 20


def test_question_limit(project_limit=None, default_limit=None):
    configured = project_limit or default_limit or MAX_TEST_QUESTIONS
    return max(1, min(MAX_TEST_QUESTIONS, int(configured)))


def allowed_category_counts(counts, distribution=None):
    if distribution:
        return {category: min(counts.get(category, 0), max(0, int(quota)))
                for category, quota in distribution.items()}
    return dict(counts)


def balanced_sample(buckets, limit=MAX_TEST_QUESTIONS, distribution=None, rng=None, shuffle=True):
    """Take one per category per round, redistributing seats as buckets empty.

    Both the within-category sample and ties between categories are random.
    The final independent shuffle hides the sampling order from participants.
    Explicit administrator quotas remain upper bounds for the named categories.
    """
    rng = rng or random.SystemRandom()
    counts = allowed_category_counts({key: len(items) for key, items in buckets.items()}, distribution)
    pools = {}
    for category, count in counts.items():
        if count:
            items = list(buckets[category])
            if shuffle:
                rng.shuffle(items)
            pools[category] = items[:count]
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
