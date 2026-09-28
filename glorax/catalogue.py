"""Fast publication-based counts for public and admin project catalogues."""

from .extensions import db
from .models import QuestionRevision, utcnow
from .question_policy import area_policy_clause
from .selection import allowed_category_counts, test_question_limit
from .settings import get_setting


def available_question_counts(projects):
    """Return quick catalogue counts from the publication-time validation.

    Deep question validation is intentionally done when starting a test and
    when publishing a dataset. Re-running it for every question across every
    project made the public home page take down all four Gunicorn request
    threads. Published questions are already validated; this grouped query
    only excludes target facts that have since expired or been superseded.
    """
    enabled = [project for project in projects if project and project.enabled]
    if not enabled:
        return {project.id: 0 for project in projects if project}
    from sqlalchemy import func, or_

    from .models import Fact, FactRevision, Question

    project_ids = [project.id for project in enabled]
    now = utcnow()
    rows = db.session.execute(
        db.select(Question.project_id, QuestionRevision.category, func.count(Question.id))
        .join(QuestionRevision, QuestionRevision.id == Question.current_revision_id)
        .join(FactRevision, FactRevision.id == QuestionRevision.target_fact_revision_id)
        .join(Fact, Fact.id == FactRevision.fact_id)
        .where(
            Question.project_id.in_(project_ids),
            Question.status == "published",
            area_policy_clause(Fact, FactRevision),
            or_(Question.origin == "generated", QuestionRevision.semantic_reviewed.is_(True)),
            Fact.current_revision_id == FactRevision.id,
            Fact.review_pending.is_(False),
            FactRevision.verification_status.in_(("verified", "manual_verified")),
            FactRevision.value.is_not(None),
            FactRevision.missing_reason.is_(None),
            FactRevision.source_url.is_not(None),
            FactRevision.evidence.is_not(None),
            or_(FactRevision.valid_until.is_(None), FactRevision.valid_until > now),
        )
        .group_by(Question.project_id, QuestionRevision.category)
    ).all()
    by_project = {}
    for project_id, category, count in rows:
        by_project.setdefault(project_id, {})[category] = int(count)

    fallback_limit = get_setting("question_limit", None)
    result = {project.id: 0 for project in projects if project}
    for project in enabled:
        categories = by_project.get(project.id, {})
        count = sum(allowed_category_counts(categories, project.topic_distribution).values())
        limit = test_question_limit(project.question_limit, fallback_limit)
        result[project.id] = min(count, limit)
    return result
