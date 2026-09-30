from flask import Blueprint

bp = Blueprint("admin", __name__, url_prefix="/admin")
from . import (
    common,
    exchange,
    operations,
    projects,
    questions,
    reports,
)
