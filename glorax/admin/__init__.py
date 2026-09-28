"""Administrative routes. Keep endpoint names stable for templates and links."""

from flask import Blueprint

bp = Blueprint("admin", __name__, url_prefix="/admin")
from . import (  # noqa: E402,F401
    common,
    exchange,
    operations,
    projects,
    questions,
    reports,
)
