"""Shared rules for the content of newly assigned tests."""

from sqlalchemy import and_, func, not_, or_

from .question_values import canonical_unit

APARTMENT_AREA_KEYS = {
    "min_area",
    "advertised_min_area",
    "max_area",
    "studio_max_area",
    "plan_area_min",
    "plan_area_max",
    "terrace_area",
    "patio_area",
}


def area_policy_error(fact, revision):
    """Keep only an explicitly scoped studio maximum among apartment areas."""
    is_area = fact.key in APARTMENT_AREA_KEYS or (
        fact.category == "layouts" and canonical_unit(revision.unit) == "m2"
    )
    studio_maximum = (
        fact.key in {"max_area", "studio_max_area"} and str((fact.scope or {}).get("rooms")) == "0"
    )
    if is_area and not studio_maximum:
        return "В тесте разрешена только максимальная площадь студий; другие вопросы о метраже квартир исключены"
    return None


def area_policy_clause(fact_model, revision_model):
    """SQL equivalent for catalogue counts, without loading the question bank."""
    is_area = or_(
        fact_model.key.in_(APARTMENT_AREA_KEYS),
        and_(
            fact_model.category == "layouts",
            revision_model.unit.in_(("м²", "м2", "кв.м", "кв. м", "m²", "m2")),
        ),
    )
    studio_maximum = and_(
        fact_model.key.in_(("max_area", "studio_max_area")),
        fact_model.scope["rooms"].as_string() == "0",
    )
    # COALESCE preserves non-area facts with NULL units/scopes under SQL's
    # three-valued logic. Missing room scope can never prove a studio maximum.
    return or_(not_(func.coalesce(is_area, False)), func.coalesce(studio_maximum, False))
