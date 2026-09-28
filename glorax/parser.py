"""Collector orchestration and backwards-compatible public parser API."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from urllib.parse import urlsplit

import requests

from .sources.common import (
    BASE_URL,
    BOOKLET_HOST,
    CATALOG_URL,
    LOG,
    MAX_BOOKLET_BYTES,
    MAX_BOOKLET_BYTES_PER_REFRESH,
    MAX_BOOKLET_PAGES,
    MAX_BOOKLET_TEXT_CHARS,
    USER_AGENT,
    SourceError,
    clean_text,
    decimal_text,
    utcnow,
    walk_dicts,
)
from .sources.decoding import (
    document_url_and_size,
    extract_flight,
    extract_pdf_text,
    parse_catalog,
    parse_detail,
    parse_landing,
)
from .sources.http import HTTPClient, RobotsRules
from .sources.normalization import collect_offer_range, make_fact, normalize_project

__all__ = [
    "SourceError",
    "utcnow",
    "clean_text",
    "decimal_text",
    "walk_dicts",
    "BASE_URL",
    "CATALOG_URL",
    "USER_AGENT",
    "LOG",
    "BOOKLET_HOST",
    "MAX_BOOKLET_BYTES",
    "MAX_BOOKLET_PAGES",
    "MAX_BOOKLET_TEXT_CHARS",
    "MAX_BOOKLET_BYTES_PER_REFRESH",
    "extract_flight",
    "parse_catalog",
    "parse_detail",
    "extract_pdf_text",
    "parse_landing",
    "document_url_and_size",
    "RobotsRules",
    "HTTPClient",
    "make_fact",
    "normalize_project",
    "collect_offer_range",
    "collect_catalog",
    "scrape",
]


def collect_catalog(client):
    pending, visited, rows, materials, expected = (
        [client.base_url + "/projects"],
        set(),
        {},
        [],
        None,
    )
    while pending:
        url = pending.pop(0)
        if url in visited:
            raise SourceError("Цикл пагинации каталога")
        if len(visited) >= 100:
            raise SourceError("Превышено ограничение 100 страниц каталога")
        visited.add(url)
        html = client.get(url)
        parsed = parse_catalog(html, url)
        expected = max(expected or 0, parsed["expected"])
        for row in parsed["rows"]:
            old = rows.get(row["projectSlug"])
            if old and old != row:
                raise SourceError("Каталог изменился во время обхода; повторите сбор")
            rows[row["projectSlug"]] = row
        materials.append(
            {
                "url": url,
                "content": json.dumps(parsed["source_payload"], ensure_ascii=False),
                "content_type": "application/json; extracted-from=next-flight",
                "fetched_at": utcnow(),
            }
        )
        if len(rows) >= expected:
            break
        pending.extend(next_url for next_url in parsed["next_urls"] if next_url not in visited)
    return (
        list(rows.values()),
        len(rows) == expected,
        {
            "expected": expected,
            "found": len(rows),
            "pages": len(visited),
            "method": "embedded_next_flight",
        },
        materials,
    )


def scrape(progress=None, client=None):
    started = utcnow()
    progress = progress or (lambda *args: None)
    progress("discovery", 0, 0, "Чтение robots.txt и каталога всех регионов")
    if client is not None:
        return _collect_projects(client, progress, started)
    with HTTPClient() as client:
        return _collect_projects(client, progress, started)


def _collect_projects(client, progress, started):
    errors, projects = [], []
    rows, complete, coverage, materials = collect_catalog(client)
    if not complete:
        errors.append(
            {
                "stage": "discovery",
                "message": "Найдены не все проекты из счётчика каталога; снимок нельзя публиковать как полный",
            }
        )
    for index, row in enumerate(rows):
        progress("collection", index, len(rows), row.get("projectName", row["projectSlug"]))
        detail, sources = None, []
        fetched = utcnow()
        url = client.base_url + "/projects/" + row["projectSlug"]
        try:
            html = client.get(url)
            final_url = getattr(client, "final_urls", {}).get(url, url)
            final_slug = urlsplit(final_url).path.rstrip("/").rsplit("/", 1)[-1]
            if urlsplit(final_url).path.startswith("/projects/"):
                detail = parse_detail(html, final_slug)
            else:
                detail = parse_landing(html, final_slug, final_url)
            sources.append(
                {
                    "url": final_url,
                    "content": json.dumps(detail, ensure_ascii=False),
                    "content_type": "application/json; extracted-from=next-flight",
                    "fetched_at": fetched,
                }
            )
        except SourceError as exc:
            errors.append(
                {"stage": "detail", "project": row["projectSlug"], "url": url, "message": str(exc)}
            )
            LOG.warning("Не удалось собрать %s: %s", row["projectSlug"], exc)
        project = normalize_project(row, detail, fetched)
        if detail:
            document_rows = (
                detail.get("documents")
                or ((detail.get("_landing_logs") or {}).get("documents") or {}).get("documents")
                or []
            )
            booklet = next(
                (
                    document
                    for document in document_rows
                    if isinstance(document, dict)
                    and "буклет" in (clean_text(document.get("title")) or "").casefold()
                ),
                None,
            )
            if booklet:
                booklet_url, declared_size = document_url_and_size(booklet)
                doc_report = {
                    "title": clean_text(booklet.get("title")),
                    "url": booklet_url,
                    "declared_size": declared_size,
                    "status": "pending_review",
                }
                try:
                    if not booklet_url:
                        raise SourceError("В источнике нет URL буклета")
                    pdf = client.get_booklet_pdf(booklet_url, declared_size)
                    pages = extract_pdf_text(pdf)
                    extracted = {
                        "pages": pages,
                        "page_count": len(pages),
                        "text_status": "extracted" if pages else "no_text_layer",
                        "review_required": True,
                        "note": "Текст PDF — исходный материал, а не подтверждённый факт; смысл и область применения нужно проверить администратору.",
                    }
                    sources.append(
                        {
                            "url": booklet_url,
                            "content": json.dumps(extracted, ensure_ascii=False),
                            "content_type": "application/pdf-extracted-text; charset=utf-8",
                            "fetched_at": fetched,
                        }
                    )
                    doc_report.update(
                        {
                            "status": extracted["text_status"],
                            "pages_with_text": len(pages),
                            "downloaded_bytes": len(pdf),
                            "sha256": hashlib.sha256(pdf).hexdigest(),
                            "review_required": True,
                        }
                    )
                except (SourceError, requests.RequestException) as exc:
                    doc_report.update({"status": "unavailable_or_skipped", "reason": str(exc)})
                    errors.append(
                        {
                            "stage": "document",
                            "project": row["projectSlug"],
                            "url": booklet_url,
                            "message": str(exc),
                        }
                    )
                    LOG.warning("Буклет %s пропущен: %s", row["projectSlug"], exc)
                project["coverage"]["booklet"] = doc_report
            else:
                project["coverage"]["booklet"] = {
                    "status": "not_published",
                    "reason": "В источнике не найден документ с названием «Буклет проекта»",
                }
        if detail:
            project["canonical_url"] = final_url
            for fact in project["facts"]:
                if fact["source_url"] == url:
                    fact["source_url"] = final_url
        # Keep original scoped catalog row; all values have exact source evidence.
        sources.insert(
            0,
            {
                "url": CATALOG_URL,
                "content": json.dumps(row, ensure_ascii=False),
                "content_type": "application/json; extracted-from=next-flight",
                "fetched_at": materials[0]["fetched_at"],
            },
        )
        project["sources"] = sources
        projects.append(project)
    coverage.update(
        {
            "detail_success": sum(p["coverage"]["detail_available"] for p in projects),
            "verified_facts": sum(p["coverage"]["verified_facts"] for p in projects),
            "review_candidates": sum(p["coverage"]["review_candidates"] for p in projects),
            "regions_or_cities": sorted(
                set(p["city"] or p["region"] or "Не указано" for p in projects)
            ),
            "status_counts": dict(Counter(p["status"] or "Не указан" for p in projects)),
            "full_offer_crawl": False,
            "robots_observed": True,
            "concurrency": 1,
            "catalog_complete": complete,
        }
    )
    # A detail failure is a partial collection: caller must retain the prior
    # published snapshot, rather than stamp old detail data with a new date.
    successful = complete and coverage["detail_success"] == len(projects)
    progress(
        "normalization",
        len(projects),
        len(projects),
        f"Проектов: {len(projects)}, ошибки: {len(errors)}",
    )
    return {
        "projects": projects,
        "complete": successful,
        "errors": errors,
        "coverage": coverage,
        "started_at": started,
        "finished_at": utcnow(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Живая проверка официального каталога GloraX")
    parser.add_argument("--output", help="JSON с материалами и фактами (без данных сотрудников)")
    args = parser.parse_args()
    result = scrape()
    if args.output:
        from pathlib import Path

        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("complete", "coverage", "errors", "started_at", "finished_at")
            },
            ensure_ascii=False,
            indent=2,
        )
    )
