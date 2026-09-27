"""Fact-backed question construction. No fallback answers and no fuzzy guessing.

An exclusive fact describes one value for an explicitly identified scope. Only
such facts can prove that a different scalar value is a wrong answer. Presence
claims (terraces, parking, etc.) deliberately fail this gate.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import timezone
from decimal import Decimal, InvalidOperation

from .extensions import db
from .models import Dataset, DatasetFact, Fact, FactRevision, Project, Question, QuestionRevision, utcnow

TEMPLATE_VERSION = "scalar-3"
VERIFIED = {"verified", "manual_verified"}
CATEGORIES = {
    "location": "Расположение", "geography": "География", "general": "Общие сведения", "overview": "О проекте",
    "prices": "Цены", "specifications": "Характеристики", "layouts": "Планировки",
    "architecture": "Архитектура", "infrastructure": "Инфраструктура",
    "transport": "Транспорт", "amenities": "Благоустройство", "timeline": "Сроки",
    "buildings": "Корпуса", "features": "Особенности", "expert": "Экспертная оценка",
    "finishing": "Отделка", "security": "Безопасность", "courtyards": "Дворы",
    "parking": "Паркинг", "storage": "Кладовые", "entrances": "Входные группы",
}
DIFFICULTIES = {"basic", "intermediate", "advanced"}
# Each template asks for a single scalar; unordered feature bags have no template.
TEMPLATES = {
    "city": "В каком городе находится проект «{project}»{scope}?",
    "region": "В каком регионе находится проект «{project}»{scope}?",
    "address": "Какой адрес указан для проекта «{project}»{scope}?",
    "class": "Какой класс указан для проекта «{project}»{scope}?",
    "project_class": "Какой класс указан для проекта «{project}»{scope}?",
    "status": "Какой статус указан для проекта «{project}»{scope}{date}?",
    "completion_date": "Какой срок сдачи указан для проекта «{project}»{scope}{date}?",
    "completion_quarter": "Какой квартал сдачи указан для проекта «{project}»{scope}{date}?",
    "handover_date": "Какой срок передачи ключей указан для проекта «{project}»{scope}{date}?",
    "min_area": "Какова минимальная площадь в проекте «{project}»{scope}{conditions}?",
    "advertised_min_area": "Какая минимальная площадь заявлена для проекта «{project}»{scope}{conditions}?",
    "max_area": "Какова максимальная площадь в проекте «{project}»{scope}{conditions}?",
    "min_floors": "Какова минимальная этажность проекта «{project}»{scope}?",
    "max_floors": "Какова максимальная этажность проекта «{project}»{scope}?",
    "floors": "Сколько этажей указано в проекте «{project}»{scope}?",
    "floor_range": "Какая этажность указана для проекта «{project}»{scope}?",
    "building_count": "Сколько корпусов указано в проекте «{project}»{scope}?",
    "ceiling_height": "Какая высота потолков указана в проекте «{project}»{scope}{conditions}?",
    "finishing": "Какой вид отделки указан в проекте «{project}»{scope}{conditions}?",
    "architect": "Кто указан автором архитектуры проекта «{project}»{scope}?",
    "architecture_style": "Какой архитектурный стиль указан для проекта «{project}»{scope}?",
    "parking_spaces": "Сколько парковочных мест указано в проекте «{project}»{scope}{conditions}?",
    "storage_count": "Сколько кладовых указано в проекте «{project}»{scope}{conditions}?",
    "school_places": "На сколько мест рассчитана школа проекта «{project}»{scope}{conditions}?",
    "kindergarten_places": "На сколько мест рассчитан детский сад проекта «{project}»{scope}{conditions}?",
    "courtyard_area": "Какова площадь двора проекта «{project}»{scope}{conditions}?",
    "park_area": "Какова площадь парка проекта «{project}»{scope}{conditions}?",
    "distance": "Какое расстояние указано для проекта «{project}»{scope}{conditions}?",
    "travel_time": "Какое время в пути указано для проекта «{project}»{scope}{conditions}?",
    "nearest_transport_station": "Какой транспортный объект указан в каталоге для проекта «{project}»{scope}{conditions}?",
    "nearest_transport_minutes": "Какое время в пути указано в каталоге для проекта «{project}»{scope}{conditions}?",
    "advertised_min_price": "Какова заявленная минимальная полная стоимость в проекте «{project}»{scope}{conditions}{date}?",
    "min_price": "Какова минимальная полная стоимость в проекте «{project}»{scope}{conditions}{date}?",
    "max_price": "Какова максимальная полная стоимость в проекте «{project}»{scope}{conditions}{date}?",
    "offer_min_price": "Какова минимальная полная стоимость среди найденных предложений проекта «{project}»{scope}{conditions}{date}?",
    "offer_max_price": "Какова максимальная полная стоимость среди найденных предложений проекта «{project}»{scope}{conditions}{date}?",
}
_SYNONYMS = {
    "дом сдан": "завершен", "сдан": "завершен", "реализован": "завершен",
    "санкт петербург": "санкт-петербург", "санкт-петербург": "санкт-петербург",
    "спб": "санкт-петербург", "г санкт-петербург": "санкт-петербург",
    "г москва": "москва", "г. москва": "москва",
    "бизнес класс": "бизнес", "бизнес-класс": "бизнес",
    "комфорт класс": "комфорт", "комфорт-класс": "комфорт",
    "премиум класс": "премиум", "премиум-класс": "премиум",
}
_UNITS = {"руб": "RUB", "руб.": "RUB", "рублей": "RUB", "₽": "RUB", "rub": "RUB",
          "м²": "m2", "м2": "m2", "кв.м": "m2", "кв. м": "m2", "m²": "m2",
          "м": "m", "метров": "m", "метра": "m", "мин": "min", "минут": "min"}


def normalize_text(value):
    value = unicodedata.normalize("NFKC", str(value)).casefold().replace("ё", "е")
    value = re.sub(r"[‐‑‒–—]", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .\t\n")
    return _SYNONYMS.get(value, value)


def canonical_unit(unit):
    return _UNITS.get(normalize_text(unit or ""), normalize_text(unit or ""))


def _decimal(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("Числовые значения должны храниться точно: строка Decimal или целое число")
    try:
        result = Decimal(str(value).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        if not result.is_finite():
            raise ValueError("Число должно быть конечным")
        return result
    except InvalidOperation as exc:
        raise ValueError("Некорректное точное число") from exc


def canonical_display(value):
    """Catch equivalent renderings, including 10 млн ₽ / 10 000 000 рублей."""
    text = normalize_text(value)
    span = re.fullmatch(r"(?:от\s*)?(\d+(?:[.,]\d+)?)\s*(?:-|до)\s*(\d+(?:[.,]\d+)?)\s*(?:этаж(?:ей|а)?|м2|м²|м|корпус(?:ов|а)?)?", text)
    if span:
        return f"range:{_decimal(span[1]).normalize()}:{_decimal(span[2]).normalize()}"
    money = re.fullmatch(r"([\d\s]+(?:[.,]\d+)?)\s*(млн|миллионов|тыс|тысяч)?\.?\s*(₽|руб(?:лей|ля|ль)?\.?|rub)", text)
    if money:
        multiplier = Decimal("1000000") if money[2] in {"млн", "миллионов"} else Decimal("1000") if money[2] in {"тыс", "тысяч"} else Decimal(1)
        return "money:RUB:" + str((_decimal(money[1]) * multiplier).normalize())
    measured = re.fullmatch(r"([\d\s]+(?:[.,]\d+)?)\s*(м2|м²|кв\.?\s*м|m2|м|метр(?:ов|а)?|мин|минут|этаж(?:ей|а)?|корпус(?:ов|а)?)", text)
    if measured:
        unit = "m2" if re.search(r"2|²|кв", measured[2]) else "min" if measured[2].startswith("мин") else "floor" if measured[2].startswith("этаж") else "building" if measured[2].startswith("корпус") else "m"
        return f"number:{unit}:{_decimal(measured[1]).normalize()}"
    return text


def canonical_value(revision):
    if revision.value_type in {"decimal", "integer", "number", "money"}:
        return f"number:{canonical_unit(revision.unit)}:{_decimal(revision.value).normalize()}"
    if isinstance(revision.value, (list, dict, bool)) or revision.value is None:
        return json.dumps(revision.value, sort_keys=True, ensure_ascii=False)
    return canonical_display(revision.value)


def format_value(revision):
    """Never round values just to fit an option: rounded distractors can coincide."""
    if revision.value_type in {"decimal", "integer", "number", "money"}:
        value = _decimal(revision.value)
        amount = format(value, "f")
        if "." in amount:
            amount = amount.rstrip("0").rstrip(".")
        whole, _, fraction = amount.partition(".")
        whole = f"{int(whole):,}".replace(",", " ")
        amount = whole + (("," + fraction) if fraction else "")
        unit = canonical_unit(revision.unit)
        label = {"RUB": "₽", "m2": "м²", "m": "м", "min": "мин"}.get(unit, revision.unit or "")
        return f"{amount} {label}".strip()
    if not isinstance(revision.value, str):
        raise ValueError("Для варианта ответа требуется одно скалярное значение")
    display=unicodedata.normalize("NFKC", revision.value).strip()
    if not display: raise ValueError("Пустой вариант ответа недопустим")
    return display


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def freshness_errors(revision, now=None):
    now = _aware(now or utcnow())
    fact = db.session.get(Fact, revision.fact_id) if revision.fact_id else None
    if fact and fact.review_pending:
        return ["Новый источник требует проверки: прежнее значение сохранено, публикация вопросов приостановлена"]
    if revision.verification_status not in VERIFIED:
        return ["Факт не подтверждён"]
    if revision.value is None or (isinstance(revision.value,str) and not revision.value.strip()) or revision.missing_reason:
        return ["Значение факта отсутствует"]
    if not revision.source_url or not revision.evidence:
        return ["Нет ссылки на источник или подтверждающего фрагмента"]
    if revision.valid_until and _aware(revision.valid_until) <= now:
        return ["Срок актуальности факта истёк"]
    return []


def _fact(revision):
    return db.session.get(Fact, revision.fact_id)


def _context(revision):
    """Only like-for-like measurements; source/sample metadata do not change units."""
    fact = _fact(revision)
    scope = fact.scope or {}
    conditions = revision.conditions or {}
    semantic_conditions = {key: value for key, value in conditions.items() if key not in {
        "offer_count", "collection_started_at", "collection_finished_at", "complete", "sample_complete",
        "snapshot_date", "as_of", "source_label", "missing_reason", "quality", "currency",
    }}
    if fact.key in {"nearest_transport_station", "nearest_transport_minutes"}:
        semantic_conditions.pop("destination", None)
    scope_kind = scope.get("type", scope.get("level", "project"))
    return (fact.category, fact.key, canonical_unit(revision.unit), scope_kind,
            scope.get("property_type", conditions.get("property_type")), scope.get("rooms"), scope.get("object_type"),
            json.dumps(semantic_conditions, sort_keys=True, ensure_ascii=False))


def price_errors(revision):
    fact = _fact(revision)
    if fact.category != "prices":
        return []
    conditions = revision.conditions or {}
    errors = []
    if revision.value_type not in {"decimal", "integer", "money"} or canonical_unit(revision.unit) != "RUB":
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
    if fact.key in {"min_price", "max_price"} and not conditions.get("sample_complete", conditions.get("complete", False)):
        errors.append("Полнота выборки не доказана; используйте факт среди найденных предложений")
    if fact.key in {"offer_min_price", "offer_max_price", "min_price", "max_price"}:
        if not conditions.get("offer_count") or not conditions.get("collection_started_at") or not conditions.get("collection_finished_at"):
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
        errors.append("Многозначные признаки и логические значения не подходят для четырёх вариантов")
    if len(revisions) != 4:
        errors.append("Нужно ровно четыре варианта")
    canonical, displayed = set(), set()
    project_name=normalize_text(db.session.get(Project,target_fact.project_id).name)
    member_ids = {row.revision_id for row in DatasetFact.query.filter_by(dataset_id=dataset_id).all()}
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
            if len(project_name)>3 and project_name in normalize_text(format_value(revision)):
                errors.append(prefix+"название целевого проекта в варианте создаёт подсказку")
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
    if any(phrase in normalize_text(revision.text) for phrase in ("все перечисленное", "ничего из перечисленного")) or any(any(phrase in normalize_text(o.get("text", "")) for phrase in ("все перечисленное", "ничего из перечисленного")) for o in (revision.options or [])):
        errors.append("Нельзя использовать всё/ничего из перечисленного")
    target = db.session.get(FactRevision, revision.target_fact_revision_id)
    if target is None:
        return ["Не найден целевой факт"]
    question = db.session.get(Question, revision.question_id)
    if question and _fact(target).project_id != question.project_id:
        errors.append("Целевой факт принадлежит другому проекту")
    if revision.category != _fact(target).category:
        errors.append("Категория вопроса не совпадает с целевым фактом")
    options = revision.options or []
    ids = [option.get("id") for option in options]
    if len(ids) != len(set(ids)) or any(not option_id for option_id in ids):
        errors.append("ID вариантов должны быть непустыми и уникальными")
    correct = next((option for option in options if option.get("id") == revision.correct_option_id), None)
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
    if (revision.category == "expert" or target.method=="expert_assessment") and "утвержденная учебная оценка" not in normalize_text(revision.text):
        errors.append("Нужно явно указать, что проверяется утверждённая учебная оценка")
    if question and question.origin in {"manual", "ai_import"} and not semantic_review:
        errors.append("Свободная формулировка и объяснение требуют явной ручной проверки")
    return list(dict.fromkeys(errors))


def _scope_text(fact):
    scope = fact.scope or {}
    labels = []
    for key, label in (("building", "корпус"), ("phase", "очередь"), ("object_type", "тип объекта")):
        if scope.get(key):
            labels.append(f"{label}: {scope[key]}")
    if scope.get("rooms") is not None:
        labels.append("студии" if str(scope["rooms"]) == "0" else "4 и более комнат" if str(scope["rooms"]) in {"4", "4+"} else f"комнат: {scope['rooms']}")
    if scope.get("property_type"):
        labels.append({"flat": "квартиры", "apartment": "апартаменты"}.get(scope["property_type"], scope["property_type"]))
    if scope.get("level") in {"building", "queue"} and scope.get("name"):
        labels.append(("корпус" if scope["level"] == "building" else "очередь") + ": " + str(scope["name"]))
    return " (" + "; ".join(labels) + ")" if labels else ""


def _conditions_text(revision):
    conditions = revision.conditions or {}
    labels = []
    property_names = {"apartment": "апартаменты", "flat": "квартиры", "apartments": "квартиры",
                      "serviced_apartment": "апартаменты", "aparthotel": "апартаменты",
                      "parking": "машино-места", "storage": "кладовые", "commercial": "коммерческие помещения"}
    for key, label in (("property_type", "тип"), ("payment_terms", "оплата"), ("promotion", "акция"),
                       ("state", "состояние"), ("destination", "до"), ("mode", "способ передвижения"),
                       ("transport_mode", "способ передвижения"), ("node_type", "тип объекта")):
        if key == "destination" and _fact(revision).key == "nearest_transport_station":
            continue
        if key in conditions and conditions[key] is not None:
            value = conditions[key]
            if key == "property_type":
                value = property_names.get(value, value)
            value = {"not_specified": "не указаны источником", "walk": "пешком", "walking": "пешком",
                     "pedestrian": "пешком", "metro": "метро", "bus": "остановка автобуса",
                     "public_transport": "общественный транспорт", "car": "автомобиль", "planned": "планируется", "existing": "существует"}.get(str(value), value)
            labels.append(f"{label}: {value}")
    return " (" + "; ".join(labels) + ")" if labels else ""


def make_text(target, project):
    fact = _fact(target)
    template = TEMPLATES.get(fact.key)
    if not template:
        raise ValueError("Нет проверенного шаблона для этой характеристики")
    if fact.key in {"distance", "travel_time", "nearest_transport_minutes"} and not ((target.conditions or {}).get("destination") and ((target.conditions or {}).get("mode") or (target.conditions or {}).get("transport_mode"))):
        raise ValueError("Для транспортного вопроса нужны место назначения и способ передвижения")
    return template.format(project=project.name, scope=_scope_text(fact),
                           date=f" по данным на {_aware(target.created_at).strftime('%d.%m.%Y')}",
                           conditions=_conditions_text(target))


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def revision_data(target, distractors, project, dataset_id):
    options = [{"id": str(index), "fact_revision_id": revision.id, "text": format_value(revision)}
               for index, revision in enumerate([target] + distractors)]
    return {"dataset_id": dataset_id, "text": make_text(target, project), "category": _fact(target).category,
            "options": options, "correct_option_id": "0", "target_fact_revision_id": target.id,
            "explanation": f"Источник: {target.source_url}. {target.evidence}", "difficulty": "basic",
            "tags": [_fact(target).key], "template_version": TEMPLATE_VERSION}


def generate_questions(dataset_id):
    """Stage revisions in the caller's transaction; the publisher commits once."""
    dataset = db.session.get(Dataset, dataset_id)
    if not dataset:
        raise ValueError("Снимок датасета не найден")
    revisions = db.session.scalars(db.select(FactRevision).join(DatasetFact, DatasetFact.revision_id == FactRevision.id)
                                   .where(DatasetFact.dataset_id == dataset_id).order_by(FactRevision.id)).all()
    facts_by_id = {f.id:f for f in Fact.query.all()}
    candidates_by_context = {}
    for candidate in revisions:
        if candidate.is_exclusive and not freshness_errors(candidate) and not price_errors(candidate) and facts_by_id[candidate.fact_id].current_revision_id == candidate.id:
            candidates_by_context.setdefault(_context(candidate), []).append(candidate)
    report = {"created": 0, "updated": 0, "unchanged": 0, "skipped": [], "needs_review": 0}
    for target in revisions:
        fact = facts_by_id[target.fact_id]
        key = f"fact:{fact.id}"
        question = Question.query.filter_by(generation_key=key).first()
        if question is None:
            question = Question.query.filter(Question.generation_key.like("%:"+fact.id), Question.origin=="generated").first()
            if question: question.generation_key=key
        reasons = freshness_errors(target) + price_errors(target)
        if not target.is_exclusive or fact.key not in TEMPLATES or (fact.category == "expert" or target.method == "expert_assessment"):
            reasons.append("Нет безопасной автоматической модели единственного ответа")
        if question and (question.status == "archived" or question.origin != "generated"):
            continue
        try:
            project = db.session.get(Project, fact.project_id)
            make_text(target, project)
        except ValueError as exc:
            reasons.append(str(exc))
        distractors, values, displays = [], set(), set()
        if not reasons:
            values.add(canonical_value(target))
            displays.add(canonical_display(format_value(target)))
            for candidate in candidates_by_context.get(_context(target), []):
                other_fact = facts_by_id[candidate.fact_id]
                if other_fact.project_id == fact.project_id or not candidate.is_exclusive or not comparable(target, candidate):
                    continue
                if freshness_errors(candidate) or price_errors(candidate) or other_fact.current_revision_id != candidate.id:
                    continue
                try:
                    value, display = canonical_value(candidate), canonical_display(format_value(candidate))
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
                reasons.append("Недостаточно трёх подтверждённых сопоставимых уникальных вариантов других проектов")
            else:
                reasons += validate_fact_options(target, [target] + distractors, dataset_id)
        if reasons:
            report["skipped"].append({"fact_revision_id": target.id, "project_id": fact.project_id, "reasons": reasons})
            if question and question.status == "published":
                question.status, question.review_reason = "needs_review", "; ".join(reasons)
                report["needs_review"] += 1
            continue
        data = revision_data(target, distractors, project, dataset_id)
        content_hash = fingerprint({key: value for key, value in data.items() if key != "dataset_id"})
        existing = db.session.get(QuestionRevision, question.current_revision_id) if question else None
        if existing and existing.fingerprint == content_hash:
            report["unchanged"] += 1
            continue
        if question is None:
            question = Question(project_id=fact.project_id, origin="generated", status="published", generation_key=key)
            db.session.add(question)
            db.session.flush()
            report["created"] += 1
        else:
            report["updated"] += 1
        revision = QuestionRevision(question_id=question.id, fingerprint=content_hash, **data)
        db.session.add(revision)
        db.session.flush()
        question.current_revision_id, question.status, question.review_reason = revision.id, "published", None
    # Existing manual/import questions are never rewritten after a source changes.
    for question in Question.query.filter(Question.status == "published").all():
        revision = db.session.get(QuestionRevision, question.current_revision_id)
        if revision:
            errors = validate_revision(revision, semantic_review=True)
            if errors:
                question.status, question.review_reason = "needs_review", "; ".join(errors)
                report["needs_review"] += 1
    db.session.flush()
    return report


def eligible_questions(project):
    project_id = project.id if hasattr(project, "id") else project
    project_obj = db.session.get(Project, project_id)
    if not project_obj or not project_obj.enabled:
        return []
    result = []
    for question in Question.query.filter_by(project_id=project_id, status="published").order_by(Question.id).all():
        revision = db.session.get(QuestionRevision, question.current_revision_id)
        if revision and (question.origin == "generated" or revision.semantic_reviewed) and not validate_revision(revision, semantic_review=True):
            result.append(question)
    return result
