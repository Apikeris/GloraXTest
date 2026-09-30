from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

BASE_URL = "https://glorax.com"


CATALOG_URL = BASE_URL + "/projects"


USER_AGENT = "GloraXKnowledgeCollector/1.0"


LOG = logging.getLogger(__name__)


BOOKLET_HOST = "cms-dev.city-digital.ru"


MAX_BOOKLET_BYTES = 30 * 1024 * 1024


MAX_BOOKLET_PAGES = 300


MAX_BOOKLET_TEXT_CHARS = 1_500_000


MAX_BOOKLET_BYTES_PER_REFRESH = 256 * 1024 * 1024


class SourceError(RuntimeError):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    if value is None or value == "$undefined":
        return None
    if not isinstance(value, str):
        value = str(value)
    if value.startswith("$"):
        return None
    return " ".join(BeautifulSoup(value, "html.parser").get_text(" ", strip=True).split()) or None


def decimal_text(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return format(parsed, "f") if parsed.is_finite() else None
    except InvalidOperation:
        return None


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)
