"""Source decoding. External content is always treated as data."""

from __future__ import annotations

import json
import re
from io import BytesIO
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, SoupStrainer

from .common import (
    CATALOG_URL,
    MAX_BOOKLET_PAGES,
    MAX_BOOKLET_TEXT_CHARS,
    SourceError,
    clean_text,
    walk_dicts,
)


def extract_flight(html):
    """Decode JSON transport only, including UTF-8 length-delimited T records.

    No JavaScript interpreter is used. Flight references can point at record
    properties; resolve only data references, with bounded depth/cycle checks.
    """
    # Only script transport is needed; building the entire marketing-page DOM
    # multiplies memory usage on a 512 MB web service.
    soup = BeautifulSoup(html, "html.parser", parse_only=SoupStrainer("script"))
    decoder = json.JSONDecoder()
    chunks = []
    for tag in soup.find_all("script"):
        script = tag.string or tag.get_text()
        for match in re.finditer(r"self\.__next_f\.push\(", script):
            try:
                payload, _ = decoder.raw_decode(script, match.end())
            except ValueError:
                continue
            if (
                isinstance(payload, list)
                and len(payload) == 2
                and payload[0] == 1
                and isinstance(payload[1], str)
            ):
                chunks.append(payload[1])
    soup.decompose()
    data = "".join(chunks).encode("utf-8")
    records, pos = {}, 0
    while pos < len(data):
        match = re.match(rb"([0-9a-f]+):", data[pos:])
        if not match:
            end = data.find(b"\n", pos)
            if end < 0:
                break
            pos = end + 1
            continue
        key = match[1].decode()
        pos += match.end()
        if data[pos : pos + 1] == b"T":
            end = data.find(b",", pos)
            if end < 0:
                break
            try:
                length = int(data[pos + 1 : end], 16)
                records[key] = data[end + 1 : end + 1 + length].decode("utf-8")
                pos = end + 1 + length
            except (ValueError, UnicodeDecodeError):
                raise SourceError("Изменился формат текстовых записей Next.js")
        else:
            end = data.find(b"\n", pos)
            if end < 0:
                end = len(data)
            try:
                records[key] = json.loads(data[pos:end])
            except (ValueError, UnicodeDecodeError):
                pass  # I/HL import records are not application data.
            pos = end + 1

    def resolve(value, seen=frozenset(), depth=0):
        if depth > 35:
            return None
        if isinstance(value, str) and re.fullmatch(r"\$[0-9a-f]+(?::[^:]+)*", value):
            if value in seen:
                return None
            bits = value[1:].split(":")
            target = records.get(bits[0])
            try:
                for bit in bits[1:]:
                    if isinstance(target, list) and bit == "props":
                        target = target[3]
                    else:
                        target = target[int(bit)] if isinstance(target, list) else target[bit]
            except (KeyError, IndexError, TypeError, ValueError):
                return None
            return resolve(target, seen | {value}, depth + 1)
        if isinstance(value, dict):
            return {k: resolve(v, seen, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, seen, depth + 1) for v in value]
        return None if value == "$undefined" else value

    # Resolve one record at a time instead of retaining every expanded React tree.
    return (resolve(value) for value in records.values())


def parse_catalog(html, url=CATALOG_URL):
    candidates = [
        item
        for record in extract_flight(html)
        for item in walk_dicts(record)
        if isinstance(item.get("data"), list)
        and "projectsCnt" in item
        and all(isinstance(row, dict) and row.get("projectSlug") for row in item["data"])
    ]
    if not candidates:
        raise SourceError(
            "Не найдена структура каталога initialProjectCatalogData; публикация отменена"
        )
    catalog = max(candidates, key=lambda item: len(item["data"]))
    unique = {row["projectSlug"]: row for row in catalog["data"]}
    expected = catalog.get("projectsCnt")
    if not isinstance(expected, int) or expected < 1 or not unique:
        raise SourceError("Каталог не содержит достоверного счётчика проектов")
    soup = BeautifulSoup(html, "html.parser", parse_only=SoupStrainer("a"))
    next_urls = []
    for element in soup.select(
        'a[rel~="next"], a[data-next-page], a[aria-label="Следующая страница"]'
    ):
        href = element.get("href") or element.get("data-next-page")
        if href:
            next_urls.append(urljoin(url, href))
    # Explicit links from real payloads only; no guessed API or page parameter.
    for key in ("next", "nextPageUrl", "next_url"):
        if isinstance(catalog.get(key), str) and catalog[key]:
            next_urls.append(urljoin(url, catalog[key]))
    return {
        "rows": list(unique.values()),
        "expected": expected,
        "next_urls": list(dict.fromkeys(next_urls)),
        "complete": len(unique) == expected,
        "source_payload": catalog,
    }


def parse_detail(html, slug):
    candidates, finishing_components = [], []
    for record in extract_flight(html):
        for item in walk_dicts(record):
            if isinstance(item.get("logs"), dict) and item["logs"].get("slug") == slug:
                candidates.append(item["logs"])
            if (
                item.get("slug") == slug
                and isinstance(item.get("data"), list)
                and any(
                    isinstance(r, dict) and "code" in r and "lotsCount" in r for r in item["data"]
                )
            ):
                finishing_components.append(item)
    if not candidates:
        raise SourceError(
            f"Нет привязанного к проекту объекта logs.slug={slug}; меню не используется"
        )
    detail = max(candidates, key=lambda item: len(json.dumps(item, ensure_ascii=False)))
    # These supplementary components are accepted only with an exact project slug.
    detail = dict(detail)
    detail["_finishing_components"] = [r for item in finishing_components for r in item["data"]]
    return detail


def extract_pdf_text(content, *, max_pages=MAX_BOOKLET_PAGES, max_chars=MAX_BOOKLET_TEXT_CHARS):
    """Extract a bounded text layer from a linked PDF; never OCR or trust it as verified facts."""
    if not content.startswith(b"%PDF-"):
        raise SourceError("Ссылка на буклет вернула не PDF")
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content), strict=False)
        if len(reader.pages) > max_pages:
            raise SourceError(f"В PDF больше {max_pages} страниц; текстовый разбор пропущен")
        pages = []
        chars = 0
        for number, page in enumerate(reader.pages, 1):
            text = clean_text(page.extract_text() or "")
            if not text:
                continue
            text = text[: max_chars - chars]
            if not text:
                break
            pages.append({"page": number, "text": text})
            chars += len(text)
            if chars >= max_chars:
                break
        return pages
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"Не удалось извлечь текст PDF ({type(exc).__name__})") from exc


def parse_landing(html, slug, final_url):
    """Premium landing uses scoped project components instead of logs.slug.

    A canonical URL plus exact projectSlug bind components to this project.
    Generic h1/menu are deliberately excluded (live pages have a wrong h1).
    """
    soup = BeautifulSoup(html, "html.parser", parse_only=SoupStrainer(["link", "title", "h1"]))
    canonical = soup.select_one('link[rel="canonical"]')
    if not canonical or urlsplit(canonical.get("href", "")).path.rstrip("/") != urlsplit(
        final_url
    ).path.rstrip("/"):
        raise SourceError("Лендинг не подтверждает canonical URL проекта")
    scoped, logs = [], []
    for record in extract_flight(html):
        for obj in walk_dicts(record):
            if obj.get("projectSlug") == slug and ("citySlug" in obj or "projectName" in obj):
                scoped.append(obj)
            candidate = obj.get("logs")
            if isinstance(candidate, dict) and parse_qs(
                urlsplit(candidate.get("projectFlatsUrl", "")).query
            ).get("project") == [slug]:
                logs.append(candidate)
    if not scoped:
        raise SourceError("Лендинг не содержит компонента с projectSlug каталога")
    chosen = max(logs, key=lambda x: len(json.dumps(x, ensure_ascii=False))) if logs else {}
    return {
        "slug": slug,
        "_landing": True,
        "_landing_logs": chosen,
        "_project_components": scoped,
        "_landing_title": soup.title.get_text(" ", strip=True) if soup.title else None,
        "_h1_ignored": [x.get_text(" ", strip=True) for x in soup.select("h1")],
        "_structured_details_available": bool(chosen),
    }


def document_url_and_size(document):
    """Read the linked-document shapes used by both detail pages and landings."""
    if not isinstance(document, dict):
        return None, None
    link = document.get("link") if isinstance(document.get("link"), dict) else {}
    return link.get("url") or document.get("url"), link.get("size") or document.get("size")
