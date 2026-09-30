from __future__ import annotations

import hashlib
import json
from datetime import timezone

from .data_access import load_by_ids
from .extensions import db
from .models import (
    Dataset,
    DatasetFact,
    Fact,
    FactRevision,
    Project,
    Question,
    QuestionRevision,
    uid,
    utcnow,
)
from .question_policy import area_policy_error
from .question_values import (
    CATEGORIES,
    DIFFICULTIES,
    TEMPLATES,
    _decimal,
    canonical_display,
    canonical_unit,
    canonical_value,
    format_value,
    normalize_text,
)

__all__ = [
    "normalize_text",
    "canonical_unit",
    "_decimal",
    "canonical_display",
    "canonical_value",
    "format_value",
    "CATEGORIES",
    "DIFFICULTIES",
    "TEMPLATES",
    "freshness_errors",
    "price_errors",
    "comparable",
    "validate_fact_options",
    "validate_revision",
    "make_text",
    "fingerprint",
    "revision_data",
    "generate_questions",
    "eligible_questions_for_projects",
    "eligible_questions",
]

TEMPLATE_VERSION = "scalar-6"


VERIFIED = {"verified", "manual_verified"}


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def freshness_errors(revision, now=None):
    now = _aware(now or utcnow())
    fact = db.session.get(Fact, revision.fact_id) if revision.fact_id else None
    if fact and fact.review_pending:
        return [
            "Новый источник требует проверки: прежнее значение сохранено, публикация вопросов приостановлена"
        ]
    if revision.verification_status not in VERIFIED:
        return ["Факт не подтверждён"]
    if (
        revision.value is None
        or (isinstance(revision.value, str) and not revision.value.strip())
        or revision.missing_reason
    ):
        return ["Значение факта отсутствует"]
    if not revision.source_url or not revision.evidence:
        return ["Нет ссылки на источник или подтверждающего фрагмента"]
    if revision.valid_until and _aware(revision.valid_until) <= now:
        return ["Срок актуальности факта истёк"]
    return []


def _fact(revision):
    return db.session.get(Fact, revision.fact_id)


def _context(revision):
    fact = _fact(revision)
    scope = fact.scope or {}
    conditions = revision.conditions or {}
    semantic_conditions = {
        key: value
        for key, value in conditions.items()
        if key
        not in {
            "offer_count",
            "collection_started_at",
            "collection_finished_at",
            "complete",
            "sample_complete",
            "snapshot_date",
            "as_of",
            "source_label",
            "missing_reason",
            "quality",
            "currency",
        }
    }
    if fact.key in {
        "nearest_transport_station",
        "nearest_transport_minutes",
        "travel_time",
        "distance",
    }:
        semantic_conditions.pop("destination", None)
    scope_kind = scope.get("type", scope.get("level", "project"))
    return (
        fact.category,
        fact.key,
        canonical_unit(revision.unit),
        scope_kind,
        scope.get("property_type", conditions.get("property_type")),
        scope.get("rooms"),
        scope.get("object_type"),
        json.dumps(semantic_conditions, sort_keys=True, ensure_ascii=False),
    )


def price_errors(revision):
    fact = _fact(revision)
    if fact.category != "prices":
        return []
    conditions = revision.conditions or {}
    errors = []
    if (
        revision.value_type not in {"decimal", "integer", "money"}
        or canonical_unit(revision.unit) != "RUB"
    ):
        errors.append("Цена должна быть точной полной стоимостью в рублях")
    if conditions.get("currency") not in {"RUB", "RUR"}:
        errors.append("Для цены не указана валюта")
    if conditions.get("price_kind", conditions.get("price_basis")) not in {"total", "full"}:
        errors.append("Не подтверждено, что это полная стоимость объекта")
    if not conditions.get("property_type"):
        errors.append("Не указан тип недвижимости")
    if "payment_terms" not in conditions:
        errors.append("Не зафиксированы условия оплаты (допускается явное значение «не указаны»)")
    if not revision.valid_until:
        errors.append("Для цены не задан срок актуальности")
    if fact.key in {"min_price", "max_price"} and not conditions.get(
        "sample_complete", conditions.get("complete", False)
    ):
        errors.append("Полнота выборки не доказана; используйте факт среди найденных предложений")
    if fact.key in {"offer_min_price", "offer_max_price", "min_price", "max_price"}:
        if (
            not conditions.get("offer_count")
            or not conditions.get("collection_started_at")
            or not conditions.get("collection_finished_at")
        ):
            errors.append("У ценовой выборки нет количества предложений и границ времени сбора")
    if fact.key == "advertised_min_price" and conditions.get("basis") != "advertised_minimum":
        errors.append("Источник не подтверждает заявленный минимум «от»")
    return errors


def comparable(target, candidate):
    return _context(target) == _context(candidate)


def validate_fact_options(target, revisions, dataset_id, require_current=True):
    errors = []
    target_fact = _fact(target)
    if not target.is_exclusive:
        errors.append("Факт не задаёт единственное значение: ложность других вариантов не доказана")
    if isinstance(target.value, (dict, list, bool)):
        errors.append(
            "Многозначные признаки и логические значения не подходят для четырёх вариантов"
        )
    if len(revisions) != 4:
        errors.append("Нужно ровно четыре варианта")
    canonical, displayed = set(), set()
    project_name = normalize_text(db.session.get(Project, target_fact.project_id).name)
    cache = db.session.info.setdefault("glorax_dataset_members_cache", {})
    if dataset_id not in cache:
        cache[dataset_id] = {
            row.revision_id for row in DatasetFact.query.filter_by(dataset_id=dataset_id).all()
        }
    member_ids = cache[dataset_id]
    for index, revision in enumerate(revisions):
        prefix = f"Вариант {index + 1}: "
        errors += [prefix + error for error in freshness_errors(revision) + price_errors(revision)]
        fact = _fact(revision)
        if revision.id not in member_ids:
            errors.append(prefix + "версия факта отсутствует в выбранном снимке")
        if require_current and fact.current_revision_id != revision.id:
            errors.append(prefix + "версия факта заменена; необходима повторная проверка")
        if not comparable(target, revision):
            errors.append(prefix + "факты несопоставимы по характеристике, единицам или условиям")
        if revision.id != target.id and fact.project_id == target_fact.project_id:
            errors.append(prefix + "неверный вариант должен происходить из другого проекта")
        try:
            value = canonical_value(revision)
            display = canonical_display(format_value(revision))
            if len(project_name) > 3 and project_name in normalize_text(format_value(revision)):
                errors.append(prefix + "название целевого проекта в варианте создаёт подсказку")
            if value in canonical or display in displayed:
                errors.append(prefix + "значение эквивалентно другому варианту")
            canonical.add(value)
            displayed.add(display)
        except ValueError as exc:
            errors.append(prefix + str(exc))
    if sum(revision.id == target.id for revision in revisions) != 1:
        errors.append("Целевой факт должен присутствовать ровно в одном варианте")
    return errors


def validate_revision(revision, require_current=True, semantic_review=False):
    errors = []
    if any(
        phrase in normalize_text(revision.text)
        for phrase in ("все перечисленное", "ничего из перечисленного")
    ) or any(
        any(
            phrase in normalize_text(o.get("text", ""))
            for phrase in ("все перечисленное", "ничего из перечисленного")
        )
        for o in (revision.options or [])
    ):
        errors.append("Нельзя использовать всё/ничего из перечисленного")
    target = db.session.get(FactRevision, revision.target_fact_revision_id)
    if target is None:
        return ["Не найден целевой факт"]
    question = db.session.get(Question, revision.question_id)
    if question and _fact(target).project_id != question.project_id:
        errors.append("Целевой факт принадлежит другому проекту")
    if revision.category != _fact(target).category:
        errors.append("Категория вопроса не совпадает с целевым фактом")
    policy_error = area_policy_error(_fact(target), target)
    if policy_error:
        errors.append(policy_error)
    options = revision.options or []
    ids = [option.get("id") for option in options]
    if len(ids) != len(set(ids)) or any(not option_id for option_id in ids):
        errors.append("ID вариантов должны быть непустыми и уникальными")
    correct = next(
        (option for option in options if option.get("id") == revision.correct_option_id), None
    )
    if not correct or correct.get("fact_revision_id") != target.id:
        errors.append("Правильный ответ должен ссылаться на целевой факт")
    facts = []
    for option in options:
        fact_revision = db.session.get(FactRevision, option.get("fact_revision_id"))
        if not fact_revision:
            errors.append("Неизвестная версия факта варианта")
        else:
            facts.append(fact_revision)
            try:
                if option.get("text") != format_value(fact_revision):
                    errors.append("Текст варианта должен формироваться сервером из факта")
            except ValueError as exc:
                errors.append(str(exc))
    errors += validate_fact_options(target, facts, revision.dataset_id, require_current)
    if (
        revision.category == "expert" or target.method == "expert_assessment"
    ) and "утвержденная учебная оценка" not in normalize_text(revision.text):
        errors.append("Нужно явно указать, что проверяется утверждённая учебная оценка")
    if question and question.origin in {"manual", "ai_import"} and not semantic_review:
        errors.append("Свободная формулировка и объяснение требуют явной ручной проверки")
    return list(dict.fromkeys(errors))


def _scope_text(fact):
    scope = fact.scope or {}
    labels = []
    for key, label in (
        ("building", "корпус"),
        ("phase", "очередь"),
        ("object_type", "тип объекта"),
    ):
        if scope.get(key):
            labels.append(f"{label}: {scope[key]}")
    if scope.get("rooms") is not None:
        room_labels = {
            "0": "студии",
            "1": "1-комнатные",
            "2": "2-комнатные",
            "3": "3-комнатные",
            "4": "4-комнатные",
            "4+": "4 и более комнат",
        }
        labels.append(room_labels.get(str(scope["rooms"]), f"комнат: {scope['rooms']}"))
    if scope.get("property_type"):
        labels.append(
            {"flat": "квартиры", "apartment": "апартаменты"}.get(
                scope["property_type"], scope["property_type"]
            )
        )
    if scope.get("level") in {"building", "queue"} and scope.get("name"):
        labels.append(
            ("корпус" if scope["level"] == "building" else "очередь") + ": " + str(scope["name"])
        )
    return " (" + "; ".join(labels) + ")" if labels else ""


def _conditions_text(revision):
    conditions = revision.conditions or {}
    labels = []
    property_names = {
        "apartment": "апартаменты",
        "flat": "квартиры",
        "apartments": "квартиры",
        "serviced_apartment": "апартаменты",
        "aparthotel": "апартаменты",
        "parking": "машино-места",
        "storage": "кладовые",
        "commercial": "коммерческие помещения",
    }
    for key, label in (
        ("property_type", "тип"),
        ("payment_terms", "оплата"),
        ("promotion", "акция"),
        ("state", "состояние"),
        ("destination", "до"),
        ("mode", "способ передвижения"),
        ("transport_mode", "способ передвижения"),
        ("node_type", "тип объекта"),
    ):
        if key == "destination" and _fact(revision).key == "nearest_transport_station":
            continue
        if key in conditions and conditions[key] is not None:
            value = conditions[key]
            if key == "property_type":
                value = property_names.get(value, value)
            value = {
                "not_specified": "не указаны источником",
                "walk": "пешком",
                "walking": "пешком",
                "pedestrian": "пешком",
                "metro": "метро",
                "bus": "остановка автобуса",
                "public_transport": "общественный транспорт",
                "car": "автомобиль",
                "planned": "планируется",
                "existing": "существует",
            }.get(str(value), value)
            labels.append(f"{label}: {value}")
    return " (" + "; ".join(labels) + ")" if labels else ""


def make_text(target, project):
    fact = _fact(target)
    template = TEMPLATES.get(fact.key)
    if template is None and fact.key.startswith(("project_stat_", "parameter_")):
        template = TEMPLATES["project_metric"]
    if (target.conditions or {}).get("basis") == "published_upper_bound":
        if fact.key == "patio_area":
            return f"Какова максимальная заявленная площадь патио в проекте «{project.name}»?"
        if fact.key == "terrace_area":
            return f"Какова максимальная заявленная площадь террас в проекте «{project.name}»?"
    if not template:
        raise ValueError("Нет проверенного шаблона для этой характеристики")
    if fact.key in {"distance", "travel_time", "nearest_transport_minutes"} and not (
        (target.conditions or {}).get("destination")
        and (
            (target.conditions or {}).get("mode") or (target.conditions or {}).get("transport_mode")
        )
    ):
        raise ValueError("Для транспортного вопроса нужны место назначения и способ передвижения")
    category = (target.conditions or {}).get("category_label")
    if fact.key == "nearby_category_count" and not category:
        raise ValueError("Для подсчёта объектов не указана категория карты")
    return template.format(
        project=project.name,
        scope=_scope_text(fact),
        category=category or "",
        date=f" по данным на {_aware(target.created_at).strftime('%d.%m.%Y')}",
        conditions=_conditions_text(target),
        metric_label=(target.conditions or {}).get("metric_label", ""),
    )


def fingerprint(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def revision_data(target, distractors, project, dataset_id):
    options = [
        {"id": str(index), "fact_revision_id": revision.id, "text": format_value(revision)}
        for index, revision in enumerate([target] + distractors)
    ]
    return {
        "dataset_id": dataset_id,
        "text": make_text(target, project),
        "category": _fact(target).category,
        "options": options,
        "correct_option_id": "0",
        "target_fact_revision_id": target.id,
        "explanation": f"Источник: {target.source_url}. {target.evidence}",
        "difficulty": "basic",
        "tags": [_fact(target).key],
        "template_version": TEMPLATE_VERSION,
    }


def generate_questions(dataset_id):
    dataset = db.session.get(Dataset, dataset_id)
    if not dataset:
        raise ValueError("Снимок датасета не найден")
    revisions = db.session.scalars(
        db.select(FactRevision)
        .join(DatasetFact, DatasetFact.revision_id == FactRevision.id)
        .where(DatasetFact.dataset_id == dataset_id)
        .order_by(FactRevision.id)
    ).all()
    facts_by_id = {f.id: f for f in Fact.query.all()}
    projects_by_id = load_by_ids(Project, (fact.project_id for fact in facts_by_id.values()))
    candidates_by_context = {}
    for candidate in revisions:
        if (
            candidate.is_exclusive
            and not freshness_errors(candidate)
            and not price_errors(candidate)
            and facts_by_id[candidate.fact_id].current_revision_id == candidate.id
        ):
            candidates_by_context.setdefault(_context(candidate), []).append(candidate)
    report = {"created": 0, "updated": 0, "unchanged": 0, "skipped": [], "needs_review": 0}
    existing_questions = db.session.scalars(
        db.select(Question).where(Question.origin == "generated")
    ).all()
    questions_by_key = {q.generation_key: q for q in existing_questions if q.generation_key}
    questions_by_legacy_suffix = {
        q.generation_key.rsplit(":", 1)[-1]: q
        for q in existing_questions
        if q.generation_key and ":" in q.generation_key
    }
    current_question_revisions = {}
    revision_ids = {q.current_revision_id for q in existing_questions if q.current_revision_id}
    if revision_ids:
        current_question_revisions = {
            r.id: r
            for r in db.session.scalars(
                db.select(QuestionRevision).where(QuestionRevision.id.in_(revision_ids))
            ).all()
        }
    question_heads = {}
    pending_question_revisions = {}
    for target in revisions:
        fact = facts_by_id[target.fact_id]
        key = f"fact:{fact.id}"
        question = questions_by_key.get(key)
        if question is None:
            question = questions_by_legacy_suffix.get(fact.id)
            if question:
                question.generation_key = key
        reasons = freshness_errors(target) + price_errors(target)
        policy_error = area_policy_error(fact, target)
        if policy_error:
            reasons.append(policy_error)
        if (
            not target.is_exclusive
            or (
                fact.key not in TEMPLATES
                and not fact.key.startswith(("project_stat_", "parameter_"))
            )
            or (fact.category == "expert" or target.method == "expert_assessment")
        ):
            reasons.append("Нет безопасной автоматической модели единственного ответа")
        if question and (question.status == "archived" or question.origin != "generated"):
            continue
        try:
            project = projects_by_id[fact.project_id]
            make_text(target, project)
        except ValueError as exc:
            reasons.append(str(exc))
        distractors, values, displays = [], set(), set()
        if not reasons:
            values.add(canonical_value(target))
            displays.add(canonical_display(format_value(target)))
            for candidate in candidates_by_context.get(_context(target), []):
                other_fact = facts_by_id[candidate.fact_id]
                if (
                    other_fact.project_id == fact.project_id
                    or not candidate.is_exclusive
                    or not comparable(target, candidate)
                ):
                    continue
                if (
                    freshness_errors(candidate)
                    or price_errors(candidate)
                    or other_fact.current_revision_id != candidate.id
                ):
                    continue
                try:
                    value, display = (
                        canonical_value(candidate),
                        canonical_display(format_value(candidate)),
                    )
                except ValueError:
                    continue
                if value in values or display in displays:
                    continue
                distractors.append(candidate)
                values.add(value)
                displays.add(display)
                if len(distractors) == 3:
                    break
            if len(distractors) != 3:
                reasons.append(
                    "Недостаточно трёх подтверждённых сопоставимых уникальных вариантов других проектов"
                )
            else:
                reasons += validate_fact_options(target, [target] + distractors, dataset_id)
        if reasons:
            report["skipped"].append(
                {"fact_revision_id": target.id, "project_id": fact.project_id, "reasons": reasons}
            )
            if question and question.status == "published":
                question.status, question.review_reason = "needs_review", "; ".join(reasons)
                report["needs_review"] += 1
            continue
        data = revision_data(target, distractors, project, dataset_id)
        content_hash = fingerprint(
            {key: value for key, value in data.items() if key != "dataset_id"}
        )
        existing = (
            current_question_revisions.get(question.current_revision_id) if question else None
        )
        if existing and existing.fingerprint == content_hash:
            report["unchanged"] += 1
            continue
        if question is None:
            question = Question(
                id=uid(),
                project_id=fact.project_id,
                origin="generated",
                status="published",
                generation_key=key,
            )
            db.session.add(question)
            questions_by_key[key] = question
            report["created"] += 1
        else:
            report["updated"] += 1
        revision = QuestionRevision(
            id=uid(), question_id=question.id, fingerprint=content_hash, **data
        )
        db.session.add(revision)
        question_heads[question.id] = (question, revision.id)
        pending_question_revisions[question.id] = revision
        question.status, question.review_reason = "published", None

    db.session.flush()
    for question, revision_id in question_heads.values():
        question.current_revision_id = revision_id
    db.session.flush()
    published = Question.query.filter(Question.status == "published").all()
    validation_revisions, validation_entities = _preload_question_validation(published)
    for question in published:
        revision = validation_revisions.get(question.current_revision_id)
        if revision:
            errors = validate_revision(revision, semantic_review=True)
            if errors:
                question.status, question.review_reason = "needs_review", "; ".join(errors)
                report["needs_review"] += 1
    db.session.flush()
    return report


def _preload_question_validation(questions, project_ids=()):
    revision_ids = {
        question.current_revision_id for question in questions if question.current_revision_id
    }
    question_revisions = (
        list(
            db.session.scalars(
                db.select(QuestionRevision).where(QuestionRevision.id.in_(revision_ids))
            )
        )
        if revision_ids
        else []
    )
    fact_revision_ids = set()
    dataset_ids = set()
    for revision in question_revisions:
        if revision.target_fact_revision_id:
            fact_revision_ids.add(revision.target_fact_revision_id)
        fact_revision_ids.update(
            option.get("fact_revision_id")
            for option in revision.options or []
            if option.get("fact_revision_id")
        )
        if revision.dataset_id:
            dataset_ids.add(revision.dataset_id)
    fact_revisions = (
        list(
            db.session.scalars(
                db.select(FactRevision).where(FactRevision.id.in_(fact_revision_ids))
            )
        )
        if fact_revision_ids
        else []
    )
    fact_ids = {revision.fact_id for revision in fact_revisions}
    facts = (
        list(db.session.scalars(db.select(Fact).where(Fact.id.in_(fact_ids)))) if fact_ids else []
    )
    related_project_ids = set(project_ids) | {fact.project_id for fact in facts}
    projects = (
        list(db.session.scalars(db.select(Project).where(Project.id.in_(related_project_ids))))
        if related_project_ids
        else []
    )
    cache = db.session.info.setdefault("glorax_dataset_members_cache", {})
    missing_datasets = dataset_ids - cache.keys()
    if missing_datasets:
        grouped = {dataset_id: set() for dataset_id in missing_datasets}
        for row in db.session.scalars(
            db.select(DatasetFact).where(DatasetFact.dataset_id.in_(missing_datasets))
        ):
            grouped[row.dataset_id].add(row.revision_id)
        cache.update(grouped)

    return {
        revision.id: revision for revision in question_revisions
    }, fact_revisions + facts + projects


def eligible_questions_for_projects(projects):
    by_id = {project.id: project for project in projects if project and project.enabled}
    result = {project.id: [] for project in projects if project}
    if not by_id:
        return result
    questions = list(
        db.session.scalars(
            db.select(Question)
            .where(Question.project_id.in_(by_id), Question.status == "published")
            .order_by(Question.project_id, Question.id)
        )
    )
    revisions_by_id, validation_entities = _preload_question_validation(questions, by_id)
    for question in questions:
        revision = revisions_by_id.get(question.current_revision_id)
        if (
            revision
            and (question.origin == "generated" or revision.semantic_reviewed)
            and not validate_revision(revision, semantic_review=True)
        ):
            result[question.project_id].append(question)
    return result


def eligible_questions(project):
    project_id = project.id if hasattr(project, "id") else project
    project_obj = project if hasattr(project, "id") else db.session.get(Project, project_id)
    if not project_obj or not project_obj.enabled:
        return []
    return eligible_questions_for_projects([project_obj]).get(project_id, [])
