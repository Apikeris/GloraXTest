"""Transaction-local configuration reads; explicit null remains distinct from absence."""

from .extensions import db
from .models import Setting


def get_setting(key, default=None):
    cache = db.session.info.setdefault("glorax_settings_cache", {})
    if key not in cache:
        row = db.session.get(Setting, key)
        cache[key] = (row is not None, row.value if row is not None else None)
    exists, value = cache[key]
    return value if exists else default


def set_setting(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        db.session.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.session.info.setdefault("glorax_settings_cache", {})[key] = (True, value)
