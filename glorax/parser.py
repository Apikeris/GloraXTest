"""Conservative public HTTP collector. Site data is parsed, never evaluated.

The current site ships the *entire* project catalog in Next.js Flight data even
though its HTML only renders the first cards. Detail facts are read exclusively
from the logs object whose slug matches the catalog project. Menu, recommended
projects and SEO text are never used as project facts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO
import json
import logging
import re
import time
from urllib.parse import urljoin, urlsplit, parse_qs

from bs4 import BeautifulSoup, SoupStrainer
import requests

BASE_URL = "https://glorax.com"
CATALOG_URL = BASE_URL + "/projects"
USER_AGENT = "GloraXKnowledgeCollector/1.0"
LOG = logging.getLogger(__name__)
BOOKLET_HOST = "cms-dev.city-digital.ru"  # actual document host linked from project pages
MAX_BOOKLET_BYTES = 30 * 1024 * 1024
MAX_BOOKLET_PAGES = 300
MAX_BOOKLET_TEXT_CHARS = 1_500_000
MAX_BOOKLET_BYTES_PER_REFRESH = 256 * 1024 * 1024


class SourceError(RuntimeError):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    if value is None or value == "$undefined":
        return None
    if not isinstance(value, str):
        value = str(value)
    if value.startswith("$"):
        return None
    return " ".join(BeautifulSoup(value, "html.parser").get_text(" ", strip=True).split()) or None


def decimal_text(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return format(parsed, "f") if parsed.is_finite() else None
    except InvalidOperation:
        return None


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def extract_flight(html):
    """Decode JSON transport only, including UTF-8 length-delimited T records.

    No JavaScript interpreter is used. Flight references can point at record
    properties; resolve only data references, with bounded depth/cycle checks.
    """
    # Only script transport is needed; building the entire marketing-page DOM
    # multiplies memory usage on a 512 MB web service.
    soup = BeautifulSoup(html, "html.parser", parse_only=SoupStrainer('script'))
    decoder = json.JSONDecoder()
    chunks = []
    for tag in soup.find_all("script"):
        script = tag.string or tag.get_text()
        for match in re.finditer(r"self\.__next_f\.push\(", script):
            try:
                payload, _ = decoder.raw_decode(script, match.end())
            except ValueError:
                continue
            if isinstance(payload, list) and len(payload) == 2 and payload[0] == 1 and isinstance(payload[1], str):
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
        if data[pos:pos + 1] == b"T":
            end = data.find(b",", pos)
            if end < 0:
                break
            try:
                length = int(data[pos + 1:end], 16)
                records[key] = data[end + 1:end + 1 + length].decode("utf-8")
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
    candidates = [item for record in extract_flight(html) for item in walk_dicts(record)
                  if isinstance(item.get("data"), list) and "projectsCnt" in item
                  and all(isinstance(row, dict) and row.get("projectSlug") for row in item["data"])]
    if not candidates:
        raise SourceError("Не найдена структура каталога initialProjectCatalogData; публикация отменена")
    catalog = max(candidates, key=lambda item: len(item["data"]))
    unique = {row["projectSlug"]: row for row in catalog["data"]}
    expected = catalog.get("projectsCnt")
    if not isinstance(expected, int) or expected < 1 or not unique:
        raise SourceError("Каталог не содержит достоверного счётчика проектов")
    soup = BeautifulSoup(html, "html.parser", parse_only=SoupStrainer('a'))
    next_urls = []
    for element in soup.select('a[rel~="next"], a[data-next-page], a[aria-label="Следующая страница"]'):
        href = element.get("href") or element.get("data-next-page")
        if href:
            next_urls.append(urljoin(url, href))
    # Explicit links from real payloads only; no guessed API or page parameter.
    for key in ("next", "nextPageUrl", "next_url"):
        if isinstance(catalog.get(key), str) and catalog[key]:
            next_urls.append(urljoin(url, catalog[key]))
    return {"rows": list(unique.values()), "expected": expected,
            "next_urls": list(dict.fromkeys(next_urls)),
            "complete": len(unique) == expected, "source_payload": catalog}


def parse_detail(html, slug):
    candidates, finishing_components = [], []
    for record in extract_flight(html):
        for item in walk_dicts(record):
            if isinstance(item.get('logs'),dict) and item['logs'].get('slug')==slug:
                candidates.append(item['logs'])
            if item.get('slug')==slug and isinstance(item.get('data'),list) and any(isinstance(r,dict) and 'code' in r and 'lotsCount' in r for r in item['data']):
                finishing_components.append(item)
    if not candidates:
        raise SourceError(f"Нет привязанного к проекту объекта logs.slug={slug}; меню не используется")
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
            text = text[:max_chars - chars]
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
    soup=BeautifulSoup(html,"html.parser",parse_only=SoupStrainer(['link','title','h1']))
    canonical=soup.select_one('link[rel="canonical"]')
    if not canonical or urlsplit(canonical.get("href", "")).path.rstrip("/") != urlsplit(final_url).path.rstrip("/"):
        raise SourceError("Лендинг не подтверждает canonical URL проекта")
    scoped,logs=[],[]
    for record in extract_flight(html):
        for obj in walk_dicts(record):
            if obj.get('projectSlug')==slug and ('citySlug' in obj or 'projectName' in obj):
                scoped.append(obj)
            candidate=obj.get("logs")
            if isinstance(candidate,dict) and parse_qs(urlsplit(candidate.get("projectFlatsUrl", "")).query).get("project")==[slug]:
                logs.append(candidate)
    if not scoped: raise SourceError("Лендинг не содержит компонента с projectSlug каталога")
    chosen=max(logs,key=lambda x:len(json.dumps(x,ensure_ascii=False))) if logs else {}
    return {"slug":slug,"_landing":True,"_landing_logs":chosen,"_project_components":scoped,
            "_landing_title":soup.title.get_text(" ",strip=True) if soup.title else None,
            "_h1_ignored":[x.get_text(" ",strip=True) for x in soup.select("h1")],
            "_structured_details_available":bool(chosen)}


class RobotsRules:
    """Wildcard-aware robots rules (longest rule wins; Allow wins a tie)."""
    def __init__(self, content):
        self.rules, self.delay = [], 0.4
        group_agents, group_rules, active_rules = [], [], False
        groups = []
        for raw in content.splitlines() + ["User-agent: __end__"]:
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (x.strip() for x in line.split(":", 1))
            key = key.casefold()
            if key == "user-agent":
                if active_rules:
                    groups.append((group_agents, group_rules))
                    group_agents, group_rules, active_rules = [], [], False
                group_agents.append(value.casefold())
            elif group_agents and key in ("allow", "disallow", "crawl-delay"):
                active_rules = True
                group_rules.append((key, value))
        specific = [(agents, rules) for agents, rules in groups if any(a != "*" and a in USER_AGENT.casefold() for a in agents)]
        for agents, rules in specific or [(a, r) for a, r in groups if "*" in a]:
            for key, value in rules:
                if key == "crawl-delay":
                    try:
                        self.delay = max(self.delay, float(value))
                    except ValueError:
                        pass
                elif value:
                    anchored = value.endswith("$")
                    expression = re.escape(value.rstrip("$")).replace(r"\*", ".*")
                    self.rules.append((len(value.replace("*", "")), key == "allow", re.compile("^" + expression + ("$" if anchored else ""))))

    def allowed(self, url):
        parsed = urlsplit(url)
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        matches = [(length, allow) for length, allow, pattern in self.rules if pattern.search(path)]
        return max(matches)[1] if matches else True


class HTTPClient:
    def __init__(self, base_url=BASE_URL, delay=0.4, retries=3):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain"})
        self.delay, self.retries, self.last_request = delay, retries, 0
        self.final_urls = {}
        self.rules = None
        self.document_rules = None
        self.document_robots_checked = False
        self.document_downloaded_bytes = 0
        self.document_unavailable = False
        self.robots_text = self.get(self.base_url + "/robots.txt", check_robots=False)
        self.rules = RobotsRules(self.robots_text)
        self.delay = max(self.delay, self.rules.delay)

    def get(self, url, check_robots=True, _redirects=0):
        if _redirects > 5: raise SourceError("Слишком много перенаправлений источника")
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc or urlsplit(url).scheme != "https":
            raise SourceError("Переход за пределы публичного HTTPS-источника запрещён")
        if check_robots and self.rules and not self.rules.allowed(url):
            raise SourceError(f"robots.txt запрещает сбор: {url}")
        for attempt in range(self.retries + 1):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            response = None
            try:
                response = self.session.get(url, timeout=(8, 30), allow_redirects=False, stream=True)
                if response.status_code in (401, 403):
                    raise SourceError(f"Источник ограничил доступ HTTP {response.status_code}; обход не выполняется")
                if 300 <= response.status_code < 400:
                    target=urljoin(url,response.headers.get("Location", ""))
                    if target == url: raise SourceError("Цикл перенаправлений источника")
                    result=self.get(target,check_robots=check_robots,_redirects=_redirects+1)
                    self.final_urls[url]=self.final_urls.get(target,target)
                    return result
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt >= self.retries:
                        raise SourceError(f"Источник недоступен HTTP {response.status_code}")
                    retry_after = response.headers.get("Retry-After", "")
                    pause = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                    time.sleep(min(max(pause, 1), 30))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 10 * 1024 * 1024:
                        raise SourceError("Ответ источника превышает ограничение 10 MiB")
                    chunks.append(chunk)
                self.final_urls[url]=url
                return b"".join(chunks).decode("utf-8")
            except requests.RequestException as exc:
                if attempt >= self.retries:
                    raise SourceError(f"Ошибка HTTP источника: {type(exc).__name__}") from exc
                time.sleep(min(2 ** attempt, 8))
            finally:
                if response is not None:
                    response.close()
        raise SourceError("Исчерпаны повторы HTTP")

    def get_booklet_pdf(self, url, expected_size=None):
        """Fetch only an explicitly linked project booklet from the observed CMS host.

        robots.txt must be readable and allow the exact asset. A failed CMS host
        disables further document requests for this run without failing HTML data.
        """
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != BOOKLET_HOST or parsed.query or
                not re.fullmatch(r"/assets/[0-9a-fA-F-]{36}\.pdf", parsed.path)):
            raise SourceError("Документ не является безопасной ссылкой на PDF-буклет GloraX")
        if expected_size and (expected_size < 1 or expected_size > MAX_BOOKLET_BYTES):
            raise SourceError(f"Размер буклета превышает ограничение {MAX_BOOKLET_BYTES // (1024 * 1024)} MiB")
        if self.document_downloaded_bytes + (expected_size or 0) > MAX_BOOKLET_BYTES_PER_REFRESH:
            raise SourceError("Достигнут лимит загрузки буклетов на один сбор; оставшиеся PDF перенесены на следующий запуск")
        if self.document_unavailable:
            raise SourceError("Хранилище PDF ранее стало недоступно в этом запуске")
        if not self.document_robots_checked:
            self.document_robots_checked = True
            try:
                response = self.session.get(f"https://{BOOKLET_HOST}/robots.txt", timeout=(4, 8),
                                            allow_redirects=False, stream=True)
                try:
                    if response.status_code != 200:
                        self.document_rules = RobotsRules("User-agent: *\nDisallow: /")
                        raise SourceError(f"robots.txt хранилища PDF недоступен (HTTP {response.status_code})")
                    chunks, length = [], 0
                    for chunk in response.iter_content(8192):
                        length += len(chunk)
                        if length > 256 * 1024:
                            self.document_rules = RobotsRules("User-agent: *\nDisallow: /")
                            raise SourceError("robots.txt хранилища PDF превышает ограничение")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    self.document_rules = RobotsRules(body.decode("utf-8", errors="replace"))
                finally:
                    response.close()
            except requests.RequestException as exc:
                self.document_rules = RobotsRules("User-agent: *\nDisallow: /")
                self.document_unavailable = True
                raise SourceError(f"Не удалось проверить robots.txt хранилища PDF ({type(exc).__name__})") from exc
        if not self.document_rules or not self.document_rules.allowed(url):
            raise SourceError("robots.txt запрещает сбор этого PDF")
        response = None
        try:
            response = self.session.get(url, timeout=(6, 20), allow_redirects=False, stream=True)
            if response.status_code in (401, 403):
                raise SourceError(f"Хранилище PDF ограничило доступ HTTP {response.status_code}; обход не выполняется")
            if response.status_code == 429 or response.status_code >= 500:
                raise SourceError(f"Хранилище PDF временно недоступно HTTP {response.status_code}")
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_BOOKLET_BYTES:
                raise SourceError("PDF превышает ограничение размера")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_BOOKLET_BYTES:
                    raise SourceError("PDF превысил ограничение размера во время загрузки")
                if self.document_downloaded_bytes + size > MAX_BOOKLET_BYTES_PER_REFRESH:
                    raise SourceError("Достигнут лимит загрузки буклетов на один сбор")
                chunks.append(chunk)
            content = b"".join(chunks)
            if expected_size and size != expected_size:
                LOG.warning("Размер PDF изменился: url=%s expected=%s received=%s", url, expected_size, size)
            self.document_downloaded_bytes += size
            return content
        except requests.RequestException as exc:
            raise SourceError(f"Ошибка загрузки PDF ({type(exc).__name__})") from exc
        finally:
            if response is not None:
                response.close()


def collect_catalog(client):
    pending, visited, rows, materials, expected = [client.base_url + "/projects"], set(), {}, [], None
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
        materials.append({"url": url, "content": json.dumps(parsed["source_payload"], ensure_ascii=False),
                          "content_type": "application/json; extracted-from=next-flight", "fetched_at": utcnow()})
        if len(rows) >= expected:
            break
        pending.extend(next_url for next_url in parsed["next_urls"] if next_url not in visited)
    return list(rows.values()), len(rows) == expected, {"expected": expected, "found": len(rows), "pages": len(visited), "method": "embedded_next_flight"}, materials


def make_fact(category, key, value, source_url, evidence, *, value_type="text", unit=None,
              scope=None, verified=True, exclusive=True, conditions=None, missing_reason=None, valid_days=None):
    if value is None or (isinstance(value,str) and not value.strip()):
        value = None
        verified = False
        missing_reason = missing_reason or "Источник не публикует значение"
    return {"category": category, "key": key, "value": value, "value_type": value_type,
            "unit": unit, "scope": scope or {"level": "project"}, "source_url": source_url,
            "evidence": evidence if isinstance(evidence, str) else json.dumps(evidence, ensure_ascii=False),
            "method": "public_next_flight_json", "verification_status": "verified" if verified else "needs_review",
            "is_exclusive": exclusive, "missing_reason": missing_reason, "conditions": conditions or {}, "valid_days": valid_days}


def normalize_project(row, detail=None, fetched_at=None):
    fetched_at = fetched_at or utcnow()
    slug = row["projectSlug"]
    url = BASE_URL + "/projects/" + slug
    city_value = clean_text(row.get("cityName"))
    is_region = bool(city_value and re.search(r"область|край|республика", city_value, re.I))
    city, region = (None, city_value) if is_region else (city_value, None)
    tags = [clean_text(tag.get("label")) for tag in row.get("tags", [])]
    facts = [make_fact("location", "city", city, CATALOG_URL, {"projectSlug": slug, "cityName": row.get("cityName")},
                       missing_reason="Каталог указывает регион, населённый пункт отдельно не задан" if is_region else None),
             make_fact("location", "region", region, CATALOG_URL, {"projectSlug": slug, "cityName": row.get("cityName")}),
             make_fact("location", "address", clean_text(row.get("address")), CATALOG_URL, {"projectSlug": slug, "address": row.get("address")})]
    status = None
    if detail:
        status = "Завершён" if detail.get("realizedProjectFlg") is True else "Анонсирован" if detail.get("soonOnSaleFlg") is True else "В продаже" if detail.get("salesStartFlg") is True else None
    if status is None:
        if "Скоро в продаже" in tags:
            status = "Анонсирован"
        elif any(tag and re.search("реализован|заверш[её]н|сдан", tag, re.I) for tag in tags):
            status = "Завершён"
    facts.append(make_fact("overview", "status", status, url if detail else CATALOG_URL,
                           {"projectSlug": slug, "tags": tags, "flags": {k: detail.get(k) for k in ("realizedProjectFlg", "soonOnSaleFlg", "salesStartFlg")} if detail else None}))
    property_types = {r.get("typeSlug") for r in row.get("flatType") or [] if r.get("typeSlug") in ("flat", "apartment")}
    price = decimal_text(row.get("visiblePriceMin")) if not row.get("hidePriceFlg") else None
    promo = [tag for tag in tags if tag and re.search(r"скид|ипотек|рассроч|плат[её]ж", tag, re.I)]
    conditions = {"currency": "RUB", "property_type": next(iter(property_types)) if len(property_types) == 1 else None,
                  "price_basis": "total", "basis": "advertised_minimum", "payment_terms": "Не раскрыты в каталоге",
                  "promotion_labels": promo, "sample_complete": False, "offer_count": row.get("lotsCnt"),
                  "observed_on": fetched_at[:10], "collection_started_at": fetched_at, "collection_finished_at": fetched_at}
    # The catalog is a marketing minimum. Promo applicability is not defined;
    # exact amount and property type are retained, but never auto-published.
    facts.append(make_fact("prices", "advertised_min_price", price, CATALOG_URL,
                           {"projectSlug": slug, "visiblePriceMin": row.get("visiblePriceMin"), "flatType": row.get("flatType"), "tags": tags},
                           value_type="decimal", unit="RUB", verified=False, conditions=conditions,
                           scope={"level": "property_type", "property_type": conditions["property_type"]}, valid_days=7,
                           missing_reason="Минимум отсутствует или скрыт источником" if price is None else None))
    facts.append(make_fact("prices", "max_price", None, CATALOG_URL, {"projectSlug": slug}, value_type="decimal", unit="RUB", conditions=conditions,
                           missing_reason="Нет полного обхода согласованной выборки предложений; надпись «от» не подтверждает максимум", valid_days=7))
    for flat_type in row.get("flatType") or []:
        scope = {"level": "property_type", "property_type": flat_type.get("typeSlug"), "rooms": flat_type.get("type")}
        facts.append(make_fact("layouts", "advertised_min_area", decimal_text(flat_type.get("square")), CATALOG_URL,
                               {"projectSlug": slug, "flatType": flat_type}, value_type="decimal", unit="м²", scope=scope, valid_days=7,
                               conditions={"basis": "advertised_minimum", "observed_on": fetched_at[:10]}))
        # The catalogue's per-room `price` is a published starting price, not
        # a maximum or a complete sample. Keep it separate by room type.
        room_price = decimal_text(flat_type.get("price")) if not row.get("hidePriceFlg") else None
        room_conditions = {"currency": "RUB", "price_basis": "total", "property_type": flat_type.get("typeSlug"),
                           "payment_terms": "not_specified", "promotion": "цена «от» в каталоге; применимость акций отдельно не подтверждена",
                           "basis": "advertised_minimum", "sample_complete": False,
                           "observed_on": fetched_at[:10], "collection_started_at": fetched_at,
                           "collection_finished_at": fetched_at}
        facts.append(make_fact("prices", "advertised_min_price", room_price, CATALOG_URL,
                               {"projectSlug": slug, "flatType": flat_type, "catalogue_tags": tags},
                               value_type="decimal", unit="RUB", scope=scope, verified=room_price is not None,
                               conditions=room_conditions, valid_days=7,
                               missing_reason="Для этого типа квартир каталог не показывает цену" if room_price is None else None))
    metros = row.get("metro") or []
    for i, transport in enumerate(metros):
        scope = {"level": "project", "transport_index": i}
        mode = transport.get("transportType")
        cond = {"transport_mode": mode, "node_type": transport.get("transportNodeType"), "destination": transport.get("station")}
        facts.append(make_fact("transport", "nearest_transport_station", clean_text(transport.get("station")), CATALOG_URL,
                               {"projectSlug": slug, "transport": transport}, scope=scope, exclusive=len(metros) == 1, conditions=cond))
        facts.append(make_fact("transport", "nearest_transport_minutes", transport.get("timeTo"), CATALOG_URL,
                               {"projectSlug": slug, "transport": transport}, value_type="integer", unit="мин", scope=scope,
                               verified=mode in ("pedestrian", "car", "public_transport"), exclusive=len(metros) == 1, conditions=cond))
    coverage = {"detail_available": bool(detail), "price_range_complete": False, "documents_for_review": [], "source_conflicts": [],
                "limitations": ["Отсутствие характеристики не подтверждает её отсутствие", "Полная выборка квартир не собрана; максимум не рассчитан", "Условия применения промо к цене требуют проверки"]}
    if detail:
        params = (detail.get("aboutProject") or {}).get("projectParams") or []
        mappings = {"корпуса": ("buildings", "building_count"), "этажность": ("buildings", "floor_range"),
                    "класс": ("overview", "project_class"), "класс проекта": ("overview", "project_class"),
                    "высота потолков": ("layouts", "ceiling_height"), "квартиры": ("layouts", "apartment_count"),
                    "количество секций": ("buildings", "section_count"),
                    "площадь благоустройства": ("amenities", "landscaping_area")}
        for param in params if isinstance(params, list) else []:
            label = clean_text(param.get("description"))
            title = clean_text(param.get("title"))
            mapping = mappings.get((label or "").casefold())
            if mapping:
                category, key = mapping
                facts.append(make_fact(category, key, title, url, param))
            elif title:
                facts.append(make_fact("overview", "parameter_" + hashlib.sha256((label or title).encode()).hexdigest()[:12], title,
                                       url, param, verified=False, exclusive=False, conditions={"label": label}))
        # The detailed project page publishes concise, project-scoped scalar
        # metrics. These are safe to quiz as their literal stated values.
        aliases = {
            "площадь участка": ("overview", "land_area", "га", "decimal"),
            "количество очередей строительства": ("buildings", "construction_phase_count", "очередей", "integer"),
            "очереди строительства": ("buildings", "construction_phase_count", "очередей", "integer"),
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
            value = raw.group(0).replace(" ", "").replace("\u00a0", "").replace(",", ".") if raw else None
            facts.append(make_fact(category, key, value, url, metric, value_type=value_type, unit=unit,
                                   verified=bool(value), conditions={"basis": "project_page_metric"},
                                   missing_reason="Число не удалось однозначно извлечь" if not value else None))
        # A visible range is two independent facts; never infer area bounds
        # from a single apartment or from an image.
        area_range = re.search(r"(\d+(?:[,.]\d+)?)\s*[-–—]\s*(\d+(?:[,.]\d+)?)\s*м[²2]", (detail.get("aboutProject") or {}).get("descriptionFull", ""), re.I)
        if area_range:
            for key, value in (("min_area", area_range.group(1)), ("max_area", area_range.group(2))):
                if not any(f["key"] == key and f["scope"].get("property_type") == "flat" for f in facts):
                    facts.append(make_fact("layouts", key, value.replace(",", "."), url,
                                           {"aboutProject": detail["aboutProject"], "matched_text": area_range.group(0)},
                                           value_type="decimal", unit="м²",
                                           scope={"level": "property_type", "property_type": "flat"}, conditions={"basis": "published_range"}))
        for building in (detail.get("hero") or {}).get("finishPercentage") or []:
            if building.get("label"):
                facts.append(make_fact("buildings", "completion_date", building.get("finishDate"), url, building, value_type="date",
                                       scope={"level": "building", "name": clean_text(building["label"])}, valid_days=90))
        for queue in (detail.get("constructionProgress") or {}).get("queues") or []:
            facts.append(make_fact("buildings", "completion_date", queue.get("finishDate"), url, {k: queue.get(k) for k in ("queueId", "title", "finishDate")}, value_type="date",
                                   scope={"level": "queue", "id": queue.get("queueId"), "name": clean_text(queue.get("title"))}, valid_days=90))
        finishings = detail.get("_finishing_components") or detail.get("finishings") or []
        for finishing in finishings if isinstance(finishings, list) else []:
            if isinstance(finishing, dict) and finishing.get("title"):
                facts.append(make_fact("finishing", "finishing_type", clean_text(finishing["title"]), url, finishing,
                                       scope={"level": "property_type", "property_type": detail.get("mainLotType"), "finishing_code": finishing.get("code")},
                                       exclusive=False))
        # Prose belongs to this project, but a human must approve semantics,
        # planned/existing distinction and any claim of absence.
        prose_sections = [("overview", "description", detail.get("aboutProject")),
                          ("layouts", "planning", detail.get("planningSolutions"))]
        for benefit in detail.get("benefits") or []:
            title = clean_text(benefit.get("title")) or ""
            category = "infrastructure"
            for pattern, candidate in [(r"архитект|фасад", "architecture"), (r"двор|бульвар|благоустр", "courtyards"),
                                       (r"планиров|спальн|террас|пентхаус|потол", "layouts"), (r"паркинг", "parking"),
                                       (r"кладов", "storage"), (r"безопас|охран", "security"), (r"лобби|вход", "entrances")]:
                if re.search(pattern, title, re.I):
                    category = candidate
                    break
            prose_sections.append((category, "benefit_" + str(benefit.get("id") or hashlib.sha256(title.encode()).hexdigest()[:12]), benefit))
        for category, key, obj in prose_sections:
            if not isinstance(obj, dict):
                continue
            parts = [clean_text(obj.get("title")), clean_text(obj.get("description")), clean_text(obj.get("descriptionFull"))]
            details = obj.get("details") or {}
            if isinstance(details, dict):
                parts += [clean_text(details.get("title")), clean_text(details.get("description"))]
            text = " — ".join(dict.fromkeys(part for part in parts if part))
            if text:
                planned = bool(re.search(r"будет|планиру|появится|предусмотр|проектиру", text, re.I))
                facts.append(make_fact(category, key, text[:12000], url, obj, verified=False, exclusive=False,
                                       conditions={"claim_type": "source_description", "infrastructure_state": "planned_or_mixed" if planned else "not_verified"}))
        for place in (detail.get("infrastructure") or {}).get("mapList") or []:
            travel = place.get("transportAvailability") or {}
            value = {"name": clean_text(place.get("name")), "minutes": travel.get("timeTo"), "transport_mode": travel.get("transportType"), "state": "not_verified"}
            facts.append(make_fact("infrastructure", "nearby_" + str(place.get("id")), value, url, place, value_type="json", verified=False, exclusive=False))
        for document in detail.get("documents") or []:
            if isinstance(document, dict):
                link = document.get("link") or {}
                coverage["documents_for_review"].append({"title": clean_text(document.get("title")), "url": link.get("url"), "reason": "PDF не интерпретируется автоматически; требуется извлечение и проверка"})
        hero_price = (detail.get("hero") or {}).get("minPrice")
        if hero_price is not None:
            coverage["source_conflicts"].append({"field": "price", "catalog_rub": price, "detail_hero_raw": hero_price,
                                                "reason": "Другая единица/округление и возможная акция; нельзя автоматически считать эквивалентом"})
        hero_transport = (detail.get("hero") or {}).get("transport") or {}
        for card in detail.get("infrastructureCards") or []:
            title = clean_text(card.get("title")) or ""
            if hero_transport.get("station") and hero_transport["station"] in title:
                travel = card.get("transportAvailability") or {}
                if travel.get("transportType") == hero_transport.get("transportType") and travel.get("timeTo") != hero_transport.get("timeTo"):
                    coverage["source_conflicts"].append({"field": "transport_minutes", "hero": hero_transport, "card": card})
                    for fact in facts:
                        if fact["key"] == "nearest_transport_minutes":
                            fact["verification_status"] = "needs_review"
                            fact["conditions"]["conflicting_source_values"] = True
    if detail and detail.get("_landing"):
        coverage["landing_format"]=True
        coverage["structured_detail_available"]=detail.get("_structured_details_available",False)
        coverage["ignored_h1"]=detail.get("_h1_ignored",[])
        coverage["limitations"].append("Лендинг: общий H1 не используется, принадлежность подтверждена canonical и projectSlug")
        logs=detail.get("_landing_logs") or {}
        if not logs:
            coverage["limitations"].append("Лендинг не предоставляет связанный logs: подробности требуют ручной проверки, сохранены только факты каталога")
        hero=logs.get("heroScreen") or {}
        if hero.get("address"):
            facts=[f for f in facts if f["key"]!="address"]
            facts.append(make_fact("location","address",clean_text(hero["address"]),url,{"projectSlug":slug,"heroScreen":hero}))
        progress=hero.get("progress") or {}
        if progress.get("isCompleted") is True:
            status="Завершён"
            facts=[f for f in facts if f["key"]!="status"]
            facts.append(make_fact("overview","status",status,url,{"projectSlug":slug,"progress":progress}))
        for section,category in [("aboutView","overview"),("houseWithHistory","architecture"),("architectureView","architecture"),("residentClub","infrastructure"),("parkingView","parking"),("apartmentLayoutsView","layouts"),("openTheDoorView","entrances"),("benefitCardsView","features")]:
            obj=logs.get(section)
            if not obj: continue
            texts=[]
            for part in walk_dicts(obj):
                for key in ("title","description","text","subtitle"):
                    value=clean_text(part.get(key))
                    if value and value not in texts: texts.append(value)
            if texts: facts.append(make_fact(category,"landing_"+section," — ".join(texts)[:12000],url,obj,verified=False,exclusive=False,conditions={"claim_type":"source_description","infrastructure_state":"not_verified"}))
        for document in (logs.get("documents") or {}).get("documents",[]):
            coverage["documents_for_review"].append({"data":document,"reason":"Документ лендинга требует извлечения и проверки"})
    # API gives technical dates while public cards show quarters. Preserve raw
    # date as a candidate; publish only the quarter, avoiding false day precision.
    quarterly=[]
    for fact in facts:
        if fact["key"]=="completion_date" and isinstance(fact["value"],str):
            try:
                date=datetime.fromisoformat(fact["value"].replace("Z","+00:00"))
            except ValueError: continue
            quarterly.append({**fact,"key":"completion_quarter","value":f"{(date.month-1)//3+1} кв. {date.year}","value_type":"string"})
            fact["verification_status"]="needs_review"
            fact["conditions"]={**fact["conditions"],"precision_note":"Техническая дата API; публичный срок указан кварталом"}
    facts.extend(quarterly)
    coverage["verified_facts"] = sum(fact["verification_status"] == "verified" and fact["value"] is not None for fact in facts)
    coverage["review_candidates"] = sum(fact["verification_status"] == "needs_review" and fact["value"] is not None for fact in facts)
    coverage["missing"] = {fact["key"]: fact["missing_reason"] for fact in facts if fact["value"] is None}
    return {"key": slug, "external_id": str(row["id"]), "canonical_url": url, "name": clean_text(row.get("projectName")),
            "city": city, "region": region, "status": status, "facts": facts, "sources": [], "coverage": coverage}


def collect_offer_range(offers, *, property_type, currency="RUB", payment_terms=None, complete=False,
                        expected_count=None, started_at=None, finished_at=None):
    """Aggregate a caller-proven homogeneous sample, never an advertised 'from'.

    This reusable validator is deliberately not wired to an invented API. The
    live source currently does not supply a permitted complete offer crawl.
    Every record must identify price basis, currency, type and payment terms.
    """
    selected, seen = [], set()
    rejected = []
    for offer in offers:
        if offer.get("property_type") != property_type or offer.get("currency") != currency or offer.get("payment_terms") != payment_terms or offer.get("price_basis") != "total":
            rejected.append(offer.get("id"))
            continue
        price = decimal_text(offer.get("price"))
        if offer.get("id") is None or price is None or Decimal(price) <= 0 or offer.get("id") in seen:
            rejected.append(offer.get("id"))
            continue
        seen.add(offer["id"])
        selected.append(Decimal(price))
    proven_complete = bool(complete and expected_count is not None and len(selected) == expected_count and not rejected)
    return {"minimum": format(min(selected), "f") if selected else None,
            "maximum": format(max(selected), "f") if selected else None,
            "count": len(selected), "expected_count": expected_count, "complete": proven_complete,
            "basis": "complete_offer_sample" if proven_complete else "found_offers_only",
            "property_type": property_type, "currency": currency, "payment_terms": payment_terms,
            "started_at": started_at, "finished_at": finished_at, "rejected_ids": rejected,
            "missing_reason": None if selected else "Нет подходящих предложений с полной ценой и условиями"}


def scrape(progress=None, client=None):
    started = utcnow()
    progress = progress or (lambda *args: None)
    errors, projects = [], []
    progress("discovery", 0, 0, "Чтение robots.txt и каталога всех регионов")
    client = client or HTTPClient()
    rows, complete, coverage, materials = collect_catalog(client)
    if not complete:
        errors.append({"stage": "discovery", "message": "Найдены не все проекты из счётчика каталога; снимок нельзя публиковать как полный"})
    for index, row in enumerate(rows):
        progress("collection", index, len(rows), row.get("projectName", row["projectSlug"]))
        detail, sources = None, []
        fetched = utcnow()
        url = client.base_url + "/projects/" + row["projectSlug"]
        try:
            html = client.get(url)
            final_url=getattr(client,"final_urls",{}).get(url,url)
            final_slug=urlsplit(final_url).path.rstrip("/").rsplit("/",1)[-1]
            if urlsplit(final_url).path.startswith("/projects/"):
                detail = parse_detail(html, final_slug)
            else:
                detail = parse_landing(html, final_slug, final_url)
            sources.append({"url": final_url, "content": json.dumps(detail, ensure_ascii=False),
                            "content_type": "application/json; extracted-from=next-flight", "fetched_at": fetched})
        except SourceError as exc:
            errors.append({"stage": "detail", "project": row["projectSlug"], "url": url, "message": str(exc)})
            LOG.warning("Не удалось собрать %s: %s", row["projectSlug"], exc)
        project = normalize_project(row, detail, fetched)
        if detail:
            document_rows = detail.get("documents") or ((detail.get("_landing_logs") or {}).get("documents") or {}).get("documents") or []
            booklet = next((document for document in document_rows if isinstance(document, dict) and
                            "буклет" in (clean_text(document.get("title")) or "").casefold()), None)
            if booklet:
                link = booklet.get("link") or {}
                booklet_url = link.get("url")
                doc_report = {"title": clean_text(booklet.get("title")), "url": booklet_url,
                              "declared_size": link.get("size"), "status": "pending_review"}
                try:
                    pdf = client.get_booklet_pdf(booklet_url, link.get("size"))
                    pages = extract_pdf_text(pdf)
                    extracted = {"pages": pages, "page_count": len(pages),
                                 "text_status": "extracted" if pages else "no_text_layer",
                                 "review_required": True,
                                 "note": "Текст PDF — исходный материал, а не подтверждённый факт; смысл и область применения нужно проверить администратору."}
                    sources.append({"url": booklet_url, "content": json.dumps(extracted, ensure_ascii=False),
                                    "content_type": "application/pdf-extracted-text; charset=utf-8", "fetched_at": fetched})
                    doc_report.update({"status": extracted["text_status"], "pages_with_text": len(pages),
                                       "downloaded_bytes": len(pdf), "sha256": hashlib.sha256(pdf).hexdigest(),
                                       "review_required": True})
                except (SourceError, requests.RequestException) as exc:
                    doc_report.update({"status": "unavailable_or_skipped", "reason": str(exc)})
                    errors.append({"stage": "document", "project": row["projectSlug"], "url": booklet_url,
                                   "message": str(exc)})
                    LOG.warning("Буклет %s пропущен: %s", row["projectSlug"], exc)
                project["coverage"]["booklet"] = doc_report
            else:
                project["coverage"]["booklet"] = {"status": "not_published",
                                                    "reason": "В источнике не найден документ с названием «Буклет проекта»"}
        if detail:
            project["canonical_url"]=final_url
            for fact in project["facts"]:
                if fact["source_url"]==url: fact["source_url"]=final_url
        # Keep original scoped catalog row; all values have exact source evidence.
        sources.insert(0, {"url": CATALOG_URL, "content": json.dumps(row, ensure_ascii=False),
                           "content_type": "application/json; extracted-from=next-flight", "fetched_at": materials[0]["fetched_at"]})
        project["sources"] = sources
        projects.append(project)
    coverage.update({"detail_success": sum(p["coverage"]["detail_available"] for p in projects),
                     "verified_facts": sum(p["coverage"]["verified_facts"] for p in projects),
                     "review_candidates": sum(p["coverage"]["review_candidates"] for p in projects),
                     "regions_or_cities": sorted(set(p["city"] or p["region"] or "Не указано" for p in projects)),
                     "status_counts": dict(Counter(p["status"] or "Не указан" for p in projects)),
                     "full_offer_crawl": False, "robots_observed": True,
                     "concurrency": 1, "catalog_complete": complete})
    # A detail failure is a partial collection: caller must retain the prior
    # published snapshot, rather than stamp old detail data with a new date.
    successful = complete and coverage["detail_success"] == len(projects)
    progress("normalization", len(projects), len(projects), f"Проектов: {len(projects)}, ошибки: {len(errors)}")
    return {"projects": projects, "complete": successful, "errors": errors, "coverage": coverage,
            "started_at": started, "finished_at": utcnow()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Живая проверка официального каталога GloraX")
    parser.add_argument("--output", help="JSON с материалами и фактами (без данных сотрудников)")
    args = parser.parse_args()
    result = scrape()
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("complete", "coverage", "errors", "started_at", "finished_at")}, ensure_ascii=False, indent=2))
