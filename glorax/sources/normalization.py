"""Source normalization. External content is always treated as data."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from decimal import Decimal

from .common import (
    BASE_URL,
    CATALOG_URL,
    clean_text,
    decimal_text,
    utcnow,
    walk_dicts,
)
from .decoding import document_url_and_size


def make_fact(
    category,
    key,
    value,
    source_url,
    evidence,
    *,
    value_type="text",
    unit=None,
    scope=None,
    verified=True,
    exclusive=True,
    conditions=None,
    missing_reason=None,
    valid_days=None,
):
    if value is None or (isinstance(value, str) and not value.strip()):
        value = None
        verified = False
        missing_reason = missing_reason or "Источник не публикует значение"
    return {
        "category": category,
        "key": key,
        "value": value,
        "value_type": value_type,
        "unit": unit,
        "scope": scope or {"level": "project"},
        "source_url": source_url,
        "evidence": evidence
        if isinstance(evidence, str)
        else json.dumps(evidence, ensure_ascii=False),
        "method": "public_next_flight_json",
        "verification_status": "verified" if verified else "needs_review",
        "is_exclusive": exclusive,
        "missing_reason": missing_reason,
        "conditions": conditions or {},
        "valid_days": valid_days,
    }


def normalize_project(row, detail=None, fetched_at=None):
    fetched_at = fetched_at or utcnow()
    slug = row["projectSlug"]
    url = BASE_URL + "/projects/" + slug
    city_value = clean_text(row.get("cityName"))
    is_region = bool(city_value and re.search(r"область|край|республика", city_value, re.I))
    city, region = (None, city_value) if is_region else (city_value, None)
    tags = [clean_text(tag.get("label")) for tag in row.get("tags", [])]
    facts = [
        make_fact(
            "location",
            "city",
            city,
            CATALOG_URL,
            {"projectSlug": slug, "cityName": row.get("cityName")},
            missing_reason="Каталог указывает регион, населённый пункт отдельно не задан"
            if is_region
            else None,
        ),
        make_fact(
            "location",
            "region",
            region,
            CATALOG_URL,
            {"projectSlug": slug, "cityName": row.get("cityName")},
        ),
        make_fact(
            "location",
            "address",
            clean_text(row.get("address")),
            CATALOG_URL,
            {"projectSlug": slug, "address": row.get("address")},
        ),
    ]
    status = None
    if detail:
        status = (
            "Завершён"
            if detail.get("realizedProjectFlg") is True
            else "Анонсирован"
            if detail.get("soonOnSaleFlg") is True
            else "В продаже"
            if detail.get("salesStartFlg") is True
            else None
        )
    if status is None:
        if "Скоро в продаже" in tags:
            status = "Анонсирован"
        elif any(tag and re.search("реализован|заверш[её]н|сдан", tag, re.I) for tag in tags):
            status = "Завершён"
    facts.append(
        make_fact(
            "overview",
            "status",
            status,
            url if detail else CATALOG_URL,
            {
                "projectSlug": slug,
                "tags": tags,
                "flags": {
                    k: detail.get(k)
                    for k in ("realizedProjectFlg", "soonOnSaleFlg", "salesStartFlg")
                }
                if detail
                else None,
            },
        )
    )
    property_types = {
        r.get("typeSlug")
        for r in row.get("flatType") or []
        if r.get("typeSlug") in ("flat", "apartment")
    }
    price = decimal_text(row.get("visiblePriceMin")) if not row.get("hidePriceFlg") else None
    promo = [tag for tag in tags if tag and re.search(r"скид|ипотек|рассроч|плат[её]ж", tag, re.I)]
    conditions = {
        "currency": "RUB",
        "property_type": next(iter(property_types)) if len(property_types) == 1 else None,
        "price_basis": "total",
        "basis": "advertised_minimum",
        "payment_terms": "Не раскрыты в каталоге",
        "promotion_labels": promo,
        "sample_complete": False,
        "offer_count": row.get("lotsCnt"),
        "observed_on": fetched_at[:10],
        "collection_started_at": fetched_at,
        "collection_finished_at": fetched_at,
    }
    # The catalog is a marketing minimum. Promo applicability is not defined;
    # exact amount and property type are retained, but never auto-published.
    facts.append(
        make_fact(
            "prices",
            "advertised_min_price",
            price,
            CATALOG_URL,
            {
                "projectSlug": slug,
                "visiblePriceMin": row.get("visiblePriceMin"),
                "flatType": row.get("flatType"),
                "tags": tags,
            },
            value_type="decimal",
            unit="RUB",
            verified=False,
            conditions=conditions,
            scope={"level": "property_type", "property_type": conditions["property_type"]},
            valid_days=7,
            missing_reason="Минимум отсутствует или скрыт источником" if price is None else None,
        )
    )
    facts.append(
        make_fact(
            "prices",
            "max_price",
            None,
            CATALOG_URL,
            {"projectSlug": slug},
            value_type="decimal",
            unit="RUB",
            conditions=conditions,
            missing_reason="Нет полного обхода согласованной выборки предложений; надпись «от» не подтверждает максимум",
            valid_days=7,
        )
    )
    for flat_type in row.get("flatType") or []:
        scope = {
            "level": "property_type",
            "property_type": flat_type.get("typeSlug"),
            "rooms": flat_type.get("type"),
        }
        facts.append(
            make_fact(
                "layouts",
                "advertised_min_area",
                decimal_text(flat_type.get("square")),
                CATALOG_URL,
                {"projectSlug": slug, "flatType": flat_type},
                value_type="decimal",
                unit="м²",
                scope=scope,
                valid_days=7,
                conditions={"basis": "advertised_minimum", "observed_on": fetched_at[:10]},
            )
        )
        # The catalogue's per-room `price` is a published starting price, not
        # a maximum or a complete sample. Keep it separate by room type.
        room_price = decimal_text(flat_type.get("price")) if not row.get("hidePriceFlg") else None
        room_conditions = {
            "currency": "RUB",
            "price_basis": "total",
            "property_type": flat_type.get("typeSlug"),
            "payment_terms": "not_specified",
            "promotion": "цена «от» в каталоге; применимость акций отдельно не подтверждена",
            "basis": "advertised_minimum",
            "sample_complete": False,
            "observed_on": fetched_at[:10],
            "collection_started_at": fetched_at,
            "collection_finished_at": fetched_at,
        }
        facts.append(
            make_fact(
                "prices",
                "advertised_min_price",
                room_price,
                CATALOG_URL,
                {"projectSlug": slug, "flatType": flat_type, "catalogue_tags": tags},
                value_type="decimal",
                unit="RUB",
                scope=scope,
                verified=room_price is not None,
                conditions=room_conditions,
                valid_days=7,
                missing_reason="Для этого типа квартир каталог не показывает цену"
                if room_price is None
                else None,
            )
        )
    metros = row.get("metro") or []
    for i, transport in enumerate(metros):
        scope = {"level": "project", "transport_index": i}
        mode = transport.get("transportType")
        cond = {
            "transport_mode": mode,
            "node_type": transport.get("transportNodeType"),
            "destination": transport.get("station"),
        }
        facts.append(
            make_fact(
                "transport",
                "nearest_transport_station",
                clean_text(transport.get("station")),
                CATALOG_URL,
                {"projectSlug": slug, "transport": transport},
                scope=scope,
                exclusive=len(metros) == 1,
                conditions=cond,
            )
        )
        facts.append(
            make_fact(
                "transport",
                "nearest_transport_minutes",
                transport.get("timeTo"),
                CATALOG_URL,
                {"projectSlug": slug, "transport": transport},
                value_type="integer",
                unit="мин",
                scope=scope,
                verified=mode in ("pedestrian", "car", "public_transport"),
                exclusive=len(metros) == 1,
                conditions=cond,
            )
        )
    coverage = {
        "detail_available": bool(detail),
        "price_range_complete": False,
        "documents_for_review": [],
        "source_conflicts": [],
        "limitations": [
            "Отсутствие характеристики не подтверждает её отсутствие",
            "Полная выборка квартир не собрана; максимум не рассчитан",
            "Условия применения промо к цене требуют проверки",
        ],
    }
    if detail:
        params = (detail.get("aboutProject") or {}).get("projectParams") or []
        mappings = {
            "корпуса": ("buildings", "building_count"),
            "этажность": ("buildings", "floor_range"),
            "класс": ("overview", "project_class"),
            "класс проекта": ("overview", "project_class"),
            "высота потолков": ("layouts", "ceiling_height"),
            "квартиры": ("layouts", "apartment_count"),
            "количество секций": ("buildings", "section_count"),
            "площадь благоустройства": ("amenities", "landscaping_area"),
        }
        for param in params if isinstance(params, list) else []:
            label = clean_text(param.get("description"))
            title = clean_text(param.get("title"))
            mapping = mappings.get((label or "").casefold())
            if mapping:
                category, key = mapping
                facts.append(make_fact(category, key, title, url, param))
            elif title:
                facts.append(
                    make_fact(
                        "overview",
                        "parameter_" + hashlib.sha256((label or title).encode()).hexdigest()[:12],
                        title,
                        url,
                        param,
                        verified=False,
                        exclusive=False,
                        conditions={"label": label},
                    )
                )
        # The detailed project page publishes concise, project-scoped scalar
        # metrics. These are safe to quiz as their literal stated values.
        aliases = {
            "площадь участка": ("overview", "land_area", "га", "decimal"),
            "количество очередей строительства": (
                "buildings",
                "construction_phase_count",
                "очередей",
                "integer",
            ),
            "очереди строительства": (
                "buildings",
                "construction_phase_count",
                "очередей",
                "integer",
            ),
            "количество секций": ("buildings", "section_count", "секций", "integer"),
            "мест в школе": ("infrastructure", "school_places", "мест", "integer"),
            "мест в 2 детских садах": ("infrastructure", "kindergarten_places", "мест", "integer"),
            "мест в детских садах": ("infrastructure", "kindergarten_places", "мест", "integer"),
        }
        emitted = {f["key"] for f in facts if f["key"] in {"section_count", "landscaping_area"}}
        for metric in (detail.get("aboutProject") or {}).get("statistics") or []:
            label = clean_text(metric.get("description"))
            title = clean_text(metric.get("title"))
            mapping = aliases.get((label or "").casefold())
            if not mapping or not title:
                continue
            category, key, unit, value_type = mapping
            if key in emitted or key == "section_count" and any(f["key"] == key for f in facts):
                continue
            emitted.add(key)
            raw = re.search(r"\d+(?:[\s\u00a0]\d{3})*(?:[,.]\d+)?", title)
            value = (
                raw.group(0).replace(" ", "").replace("\u00a0", "").replace(",", ".")
                if raw
                else None
            )
            facts.append(
                make_fact(
                    category,
                    key,
                    value,
                    url,
                    metric,
                    value_type=value_type,
                    unit=unit,
                    verified=bool(value),
                    conditions={"basis": "project_page_metric"},
                    missing_reason="Число не удалось однозначно извлечь" if not value else None,
                )
            )
        # A visible range is two independent facts; never infer area bounds
        # from a single apartment or from an image.
        area_range = re.search(
            r"(\d+(?:[,.]\d+)?)\s*[-–—]\s*(\d+(?:[,.]\d+)?)\s*м[²2]",
            (detail.get("aboutProject") or {}).get("descriptionFull", ""),
            re.I,
        )
        if area_range:
            for key, value in (
                ("min_area", area_range.group(1)),
                ("max_area", area_range.group(2)),
            ):
                if not any(
                    f["key"] == key and f["scope"].get("property_type") == "flat" for f in facts
                ):
                    facts.append(
                        make_fact(
                            "layouts",
                            key,
                            value.replace(",", "."),
                            url,
                            {
                                "aboutProject": detail["aboutProject"],
                                "matched_text": area_range.group(0),
                            },
                            value_type="decimal",
                            unit="м²",
                            scope={"level": "property_type", "property_type": "flat"},
                            conditions={"basis": "published_range"},
                        )
                    )
        for building in (detail.get("hero") or {}).get("finishPercentage") or []:
            if building.get("label"):
                facts.append(
                    make_fact(
                        "buildings",
                        "completion_date",
                        building.get("finishDate"),
                        url,
                        building,
                        value_type="date",
                        scope={"level": "building", "name": clean_text(building["label"])},
                        valid_days=90,
                    )
                )
        for queue in (detail.get("constructionProgress") or {}).get("queues") or []:
            facts.append(
                make_fact(
                    "buildings",
                    "completion_date",
                    queue.get("finishDate"),
                    url,
                    {k: queue.get(k) for k in ("queueId", "title", "finishDate")},
                    value_type="date",
                    scope={
                        "level": "queue",
                        "id": queue.get("queueId"),
                        "name": clean_text(queue.get("title")),
                    },
                    valid_days=90,
                )
            )
        finishings = detail.get("_finishing_components") or detail.get("finishings") or []
        for finishing in finishings if isinstance(finishings, list) else []:
            if isinstance(finishing, dict) and finishing.get("title"):
                facts.append(
                    make_fact(
                        "finishing",
                        "finishing_type",
                        clean_text(finishing["title"]),
                        url,
                        finishing,
                        scope={
                            "level": "property_type",
                            "property_type": detail.get("mainLotType"),
                            "finishing_code": finishing.get("code"),
                        },
                        exclusive=False,
                    )
                )
        # Prose belongs to this project, but a human must approve semantics,
        # planned/existing distinction and any claim of absence.
        prose_sections = [
            ("overview", "description", detail.get("aboutProject")),
            ("layouts", "planning", detail.get("planningSolutions")),
        ]
        for benefit in detail.get("benefits") or []:
            title = clean_text(benefit.get("title")) or ""
            category = "infrastructure"
            for pattern, candidate in [
                (r"архитект|фасад", "architecture"),
                (r"двор|бульвар|благоустр", "courtyards"),
                (r"планиров|спальн|террас|пентхаус|потол", "layouts"),
                (r"паркинг", "parking"),
                (r"кладов", "storage"),
                (r"безопас|охран", "security"),
                (r"лобби|вход", "entrances"),
            ]:
                if re.search(pattern, title, re.I):
                    category = candidate
                    break
            prose_sections.append(
                (
                    category,
                    "benefit_"
                    + str(benefit.get("id") or hashlib.sha256(title.encode()).hexdigest()[:12]),
                    benefit,
                )
            )
        for category, key, obj in prose_sections:
            if not isinstance(obj, dict):
                continue
            parts = [
                clean_text(obj.get("title")),
                clean_text(obj.get("description")),
                clean_text(obj.get("descriptionFull")),
            ]
            details = obj.get("details") or {}
            if isinstance(details, dict):
                parts += [clean_text(details.get("title")), clean_text(details.get("description"))]
            text = " — ".join(dict.fromkeys(part for part in parts if part))
            if text:
                planned = bool(
                    re.search(r"будет|планиру|появится|предусмотр|проектиру", text, re.I)
                )
                facts.append(
                    make_fact(
                        category,
                        key,
                        text[:12000],
                        url,
                        obj,
                        verified=False,
                        exclusive=False,
                        conditions={
                            "claim_type": "source_description",
                            "infrastructure_state": "planned_or_mixed"
                            if planned
                            else "not_verified",
                        },
                    )
                )
        for place in (detail.get("infrastructure") or {}).get("mapList") or []:
            travel = place.get("transportAvailability") or {}
            value = {
                "name": clean_text(place.get("name")),
                "minutes": travel.get("timeTo"),
                "transport_mode": travel.get("transportType"),
                "state": "not_verified",
            }
            facts.append(
                make_fact(
                    "infrastructure",
                    "nearby_" + str(place.get("id")),
                    value,
                    url,
                    place,
                    value_type="json",
                    verified=False,
                    exclusive=False,
                )
            )
        # Exact counts are useful, project-scoped quiz facts only when the
        # site's own total proves that its map list is complete. An empty or
        # truncated list must never be interpreted as zero.
        infrastructure = detail.get("infrastructure") or {}
        map_list = infrastructure.get("mapList") or []
        object_count = infrastructure.get("objectCount")
        valid_map = (
            isinstance(object_count, int)
            and not isinstance(object_count, bool)
            and object_count > 0
            and len(map_list) == object_count
            and all(
                isinstance(place, dict)
                and place.get("id") is not None
                and clean_text(place.get("name"))
                and clean_text(place.get("categoryType"))
                for place in map_list
            )
            and len({str(place["id"]) for place in map_list}) == len(map_list)
        )
        if valid_map:
            destination_names = Counter(
                unicodedata.normalize("NFKC", clean_text(place["name"])).casefold()
                for place in map_list
            )
            for place in map_list:
                travel = place.get("transportAvailability") or {}
                minutes, mode, destination = (
                    travel.get("timeTo"),
                    clean_text(travel.get("transportType")),
                    clean_text(place.get("name")),
                )
                destination_key = (
                    unicodedata.normalize("NFKC", destination).casefold() if destination else ""
                )
                if (
                    isinstance(minutes, int)
                    and not isinstance(minutes, bool)
                    and minutes > 0
                    and mode
                    and destination
                    and destination_names[destination_key] == 1
                ):
                    facts.append(
                        make_fact(
                            "transport",
                            "travel_time",
                            str(minutes),
                            url,
                            place,
                            value_type="integer",
                            unit="мин",
                            exclusive=True,
                            scope={
                                "level": "destination",
                                "id": str(place["id"]),
                                "name": destination,
                            },
                            conditions={
                                "basis": "project_infrastructure_map",
                                "destination": destination,
                                "mode": mode,
                                "map_category_type": clean_text(place.get("categoryType")),
                            },
                        )
                    )
            count_groups = {"all": ("объектов", len(map_list))}
            labels = {
                "shops": "магазинов и торговых объектов",
                "relaxSites": "мест отдыха",
                "kinderGardens": "детских садов",
                "sport": "спортивных объектов",
                "medicine": "медицинских учреждений",
                "education": "образовательных учреждений",
                "additionalEducation": "организаций дополнительного образования",
                "restaurants": "ресторанов и кафе",
                "transport": "транспортных объектов",
                "culture": "культурных объектов",
            }
            category_counts = Counter(clean_text(place.get("categoryType")) for place in map_list)
            for category_type, count in category_counts.items():
                if category_type in labels:
                    count_groups[category_type] = (labels[category_type], count)
            for category_type, (label, count) in count_groups.items():
                facts.append(
                    make_fact(
                        "infrastructure",
                        "nearby_category_count",
                        str(count),
                        url,
                        {
                            "objectCount": object_count,
                            "mapList_ids": [str(p["id"]) for p in map_list],
                            "matched_count": count,
                            "category_type": category_type,
                            "category_label": label,
                            "completeness_check": "objectCount_equals_unique_mapList_ids",
                        },
                        value_type="integer",
                        verified=True,
                        exclusive=True,
                        conditions={
                            "basis": "complete_project_infrastructure_map",
                            "category_type": category_type,
                            "category_label": label,
                        },
                    )
                )
        for document in detail.get("documents") or []:
            if isinstance(document, dict):
                document_url, _ = document_url_and_size(document)
                coverage["documents_for_review"].append(
                    {
                        "title": clean_text(document.get("title")),
                        "url": document_url,
                        "reason": "PDF не интерпретируется автоматически; требуется извлечение и проверка",
                    }
                )
        hero_price = (detail.get("hero") or {}).get("minPrice")
        if hero_price is not None:
            coverage["source_conflicts"].append(
                {
                    "field": "price",
                    "catalog_rub": price,
                    "detail_hero_raw": hero_price,
                    "reason": "Другая единица/округление и возможная акция; нельзя автоматически считать эквивалентом",
                }
            )
        hero_transport = (detail.get("hero") or {}).get("transport") or {}
        for card in detail.get("infrastructureCards") or []:
            title = clean_text(card.get("title")) or ""
            if hero_transport.get("station") and hero_transport["station"] in title:
                travel = card.get("transportAvailability") or {}
                if travel.get("transportType") == hero_transport.get(
                    "transportType"
                ) and travel.get("timeTo") != hero_transport.get("timeTo"):
                    coverage["source_conflicts"].append(
                        {"field": "transport_minutes", "hero": hero_transport, "card": card}
                    )
                    for fact in facts:
                        if fact["key"] == "nearest_transport_minutes":
                            fact["verification_status"] = "needs_review"
                            fact["conditions"]["conflicting_source_values"] = True
    if detail and detail.get("_landing"):
        coverage["landing_format"] = True
        coverage["structured_detail_available"] = detail.get("_structured_details_available", False)
        coverage["ignored_h1"] = detail.get("_h1_ignored", [])
        coverage["limitations"].append(
            "Лендинг: общий H1 не используется, принадлежность подтверждена canonical и projectSlug"
        )
        logs = detail.get("_landing_logs") or {}
        if not logs:
            coverage["limitations"].append(
                "Лендинг не предоставляет связанный logs: подробности требуют ручной проверки, сохранены только факты каталога"
            )
        hero = logs.get("heroScreen") or {}
        if hero.get("address"):
            facts = [f for f in facts if f["key"] != "address"]
            facts.append(
                make_fact(
                    "location",
                    "address",
                    clean_text(hero["address"]),
                    url,
                    {"projectSlug": slug, "heroScreen": hero},
                )
            )
        progress = hero.get("progress") or {}
        if progress.get("isCompleted") is True:
            status = "Завершён"
            facts = [f for f in facts if f["key"] != "status"]
            facts.append(
                make_fact(
                    "overview", "status", status, url, {"projectSlug": slug, "progress": progress}
                )
            )
        for section, category in [
            ("aboutView", "overview"),
            ("houseWithHistory", "architecture"),
            ("architectureView", "architecture"),
            ("residentClub", "infrastructure"),
            ("parkingView", "parking"),
            ("apartmentLayoutsView", "layouts"),
            ("openTheDoorView", "entrances"),
            ("benefitCardsView", "features"),
        ]:
            obj = logs.get(section)
            if not obj:
                continue
            texts = []
            for part in walk_dicts(obj):
                for key in ("title", "description", "text", "subtitle"):
                    value = clean_text(part.get(key))
                    if value and value not in texts:
                        texts.append(value)
            if texts:
                facts.append(
                    make_fact(
                        category,
                        "landing_" + section,
                        " — ".join(texts)[:12000],
                        url,
                        obj,
                        verified=False,
                        exclusive=False,
                        conditions={
                            "claim_type": "source_description",
                            "infrastructure_state": "not_verified",
                        },
                    )
                )
        for document in (logs.get("documents") or {}).get("documents", []):
            coverage["documents_for_review"].append(
                {"data": document, "reason": "Документ лендинга требует извлечения и проверки"}
            )
    # API gives technical dates while public cards show quarters. Preserve raw
    # date as a candidate; publish only the quarter, avoiding false day precision.
    quarterly = []
    for fact in facts:
        if fact["key"] == "completion_date" and isinstance(fact["value"], str):
            try:
                date = datetime.fromisoformat(fact["value"].replace("Z", "+00:00"))
            except ValueError:
                continue
            quarterly.append(
                {
                    **fact,
                    "key": "completion_quarter",
                    "value": f"{(date.month - 1) // 3 + 1} кв. {date.year}",
                    "value_type": "string",
                }
            )
            fact["verification_status"] = "needs_review"
            fact["conditions"] = {
                **fact["conditions"],
                "precision_note": "Техническая дата API; публичный срок указан кварталом",
            }
    facts.extend(quarterly)
    coverage["verified_facts"] = sum(
        fact["verification_status"] == "verified" and fact["value"] is not None for fact in facts
    )
    coverage["review_candidates"] = sum(
        fact["verification_status"] == "needs_review" and fact["value"] is not None
        for fact in facts
    )
    coverage["missing"] = {
        fact["key"]: fact["missing_reason"] for fact in facts if fact["value"] is None
    }
    return {
        "key": slug,
        "external_id": str(row["id"]),
        "canonical_url": url,
        "name": clean_text(row.get("projectName")),
        "city": city,
        "region": region,
        "status": status,
        "facts": facts,
        "sources": [],
        "coverage": coverage,
    }


def collect_offer_range(
    offers,
    *,
    property_type,
    currency="RUB",
    payment_terms=None,
    complete=False,
    expected_count=None,
    started_at=None,
    finished_at=None,
):
    """Aggregate a caller-proven homogeneous sample, never an advertised 'from'.

    This reusable validator is deliberately not wired to an invented API. The
    live source currently does not supply a permitted complete offer crawl.
    Every record must identify price basis, currency, type and payment terms.
    """
    selected, seen = [], set()
    rejected = []
    for offer in offers:
        if (
            offer.get("property_type") != property_type
            or offer.get("currency") != currency
            or offer.get("payment_terms") != payment_terms
            or offer.get("price_basis") != "total"
        ):
            rejected.append(offer.get("id"))
            continue
        price = decimal_text(offer.get("price"))
        if (
            offer.get("id") is None
            or price is None
            or Decimal(price) <= 0
            or offer.get("id") in seen
        ):
            rejected.append(offer.get("id"))
            continue
        seen.add(offer["id"])
        selected.append(Decimal(price))
    proven_complete = bool(
        complete and expected_count is not None and len(selected) == expected_count and not rejected
    )
    return {
        "minimum": format(min(selected), "f") if selected else None,
        "maximum": format(max(selected), "f") if selected else None,
        "count": len(selected),
        "expected_count": expected_count,
        "complete": proven_complete,
        "basis": "complete_offer_sample" if proven_complete else "found_offers_only",
        "property_type": property_type,
        "currency": currency,
        "payment_terms": payment_terms,
        "started_at": started_at,
        "finished_at": finished_at,
        "rejected_ids": rejected,
        "missing_reason": None
        if selected
        else "Нет подходящих предложений с полной ценой и условиями",
    }
