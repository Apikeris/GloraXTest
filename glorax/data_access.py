from dataclasses import dataclass

from .extensions import db
from .models import Fact, Project


def load_by_ids(model, ids):
    ids = {value for value in ids if value is not None}
    if not ids:
        return {}
    return {row.id: row for row in db.session.scalars(db.select(model).where(model.id.in_(ids)))}


@dataclass
class FactContext:
    facts: dict
    projects: dict


def load_fact_context(revisions):
    facts = load_by_ids(Fact, (revision.fact_id for revision in revisions))
    projects = load_by_ids(Project, (fact.project_id for fact in facts.values()))
    return FactContext(facts, projects)


def literal_search(value):
    return value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
