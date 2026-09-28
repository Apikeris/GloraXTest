"""Pure question templates, labels, exact formatting and equivalence rules."""

from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

CATEGORIES = {
    "location": "Расположение",
    "geography": "География",
    "general": "Общие сведения",
    "overview": "О проекте",
    "prices": "Цены",
    "specifications": "Характеристики",
    "layouts": "Планировки",
    "architecture": "Архитектура",
    "infrastructure": "Инфраструктура",
    "transport": "Транспорт",
    "amenities": "Благоустройство",
    "timeline": "Сроки",
    "buildings": "Корпуса",
    "features": "Особенности",
    "expert": "Экспертная оценка",
    "finishing": "Отделка",
    "security": "Безопасность",
    "courtyards": "Дворы",
    "parking": "Паркинг",
    "storage": "Кладовые",
    "entrances": "Входные группы",
}


DIFFICULTIES = {"basic", "intermediate", "advanced"}


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
    "land_area": "Какова площадь участка проекта «{project}»{scope}{conditions}?",
    "landscaping_area": "Какова заявленная площадь благоустройства проекта «{project}»{scope}{conditions}?",
    "section_count": "Сколько секций предусмотрено в проекте «{project}»{scope}?",
    "construction_phase_count": "На сколько очередей разделено строительство проекта «{project}»{scope}?",
    "school_places": "На сколько мест рассчитана школа проекта «{project}»{scope}{conditions}?",
    "kindergarten_places": "На сколько мест рассчитан детский сад проекта «{project}»{scope}{conditions}?",
    "courtyard_area": "Какова площадь двора проекта «{project}»{scope}{conditions}?",
    "park_area": "Какова площадь парка проекта «{project}»{scope}{conditions}?",
    "terrace_area": "Какова заявленная площадь террас проекта «{project}»{scope}{conditions}?",
    "patio_area": "Какова заявленная площадь патио проекта «{project}»{scope}{conditions}?",
    "distance": "Какое расстояние указано для проекта «{project}»{scope}{conditions}?",
    "travel_time": "Какое время в пути до объекта на карте указано для проекта «{project}»{conditions}?",
    "nearest_transport_station": "Какой транспортный объект указан в каталоге для проекта «{project}»{scope}{conditions}?",
    "nearest_transport_minutes": "Какое время в пути указано в каталоге для проекта «{project}»{scope}{conditions}?",
    "nearby_category_count": "Сколько {category} перечислено на карте инфраструктуры проекта «{project}»?",
    "advertised_min_price": "Какова заявленная минимальная полная стоимость в проекте «{project}»{scope}{conditions}{date}?",
    "min_price": "Какова минимальная полная стоимость в проекте «{project}»{scope}{conditions}{date}?",
    "max_price": "Какова максимальная полная стоимость в проекте «{project}»{scope}{conditions}{date}?",
    "offer_min_price": "Какова минимальная полная стоимость среди найденных предложений проекта «{project}»{scope}{conditions}{date}?",
    "offer_max_price": "Какова максимальная полная стоимость среди найденных предложений проекта «{project}»{scope}{conditions}{date}?",
}


_SYNONYMS = {
    "дом сдан": "завершен",
    "сдан": "завершен",
    "реализован": "завершен",
    "санкт петербург": "санкт-петербург",
    "санкт-петербург": "санкт-петербург",
    "спб": "санкт-петербург",
    "г санкт-петербург": "санкт-петербург",
    "г москва": "москва",
    "г. москва": "москва",
    "бизнес класс": "бизнес",
    "бизнес-класс": "бизнес",
    "комфорт класс": "комфорт",
    "комфорт-класс": "комфорт",
    "премиум класс": "премиум",
    "премиум-класс": "премиум",
}


_UNITS = {
    "руб": "RUB",
    "руб.": "RUB",
    "рублей": "RUB",
    "₽": "RUB",
    "rub": "RUB",
    "м²": "m2",
    "м2": "m2",
    "кв.м": "m2",
    "кв. м": "m2",
    "m²": "m2",
    "м": "m",
    "метров": "m",
    "метра": "m",
    "мин": "min",
    "минут": "min",
}


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
    span = re.fullmatch(
        r"(?:от\s*)?(\d+(?:[.,]\d+)?)\s*(?:-|до)\s*(\d+(?:[.,]\d+)?)\s*(?:этаж(?:ей|а)?|м2|м²|м|корпус(?:ов|а)?)?",
        text,
    )
    if span:
        return f"range:{_decimal(span[1]).normalize()}:{_decimal(span[2]).normalize()}"
    money = re.fullmatch(
        r"([\d\s]+(?:[.,]\d+)?)\s*(млн|миллионов|тыс|тысяч)?\.?\s*(₽|руб(?:лей|ля|ль)?\.?|rub)",
        text,
    )
    if money:
        multiplier = (
            Decimal("1000000")
            if money[2] in {"млн", "миллионов"}
            else Decimal("1000")
            if money[2] in {"тыс", "тысяч"}
            else Decimal(1)
        )
        return "money:RUB:" + str((_decimal(money[1]) * multiplier).normalize())
    measured = re.fullmatch(
        r"([\d\s]+(?:[.,]\d+)?)\s*(м2|м²|кв\.?\s*м|m2|м|метр(?:ов|а)?|мин|минут|этаж(?:ей|а)?|корпус(?:ов|а)?)",
        text,
    )
    if measured:
        unit = (
            "m2"
            if re.search(r"2|²|кв", measured[2])
            else "min"
            if measured[2].startswith("мин")
            else "floor"
            if measured[2].startswith("этаж")
            else "building"
            if measured[2].startswith("корпус")
            else "m"
        )
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
    display = unicodedata.normalize("NFKC", revision.value).strip()
    if not display:
        raise ValueError("Пустой вариант ответа недопустим")
    return display
