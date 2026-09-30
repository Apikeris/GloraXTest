from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlsplit

import requests

from .common import (
    BASE_URL,
    BOOKLET_HOST,
    LOG,
    MAX_BOOKLET_BYTES,
    MAX_BOOKLET_BYTES_PER_REFRESH,
    USER_AGENT,
    SourceError,
)


class RobotsRules:
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
        specific = [
            (agents, rules)
            for agents, rules in groups
            if any(a != "*" and a in USER_AGENT.casefold() for a in agents)
        ]
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
                    self.rules.append(
                        (
                            len(value.replace("*", "")),
                            key == "allow",
                            re.compile("^" + expression + ("$" if anchored else "")),
                        )
                    )

    def allowed(self, url):
        parsed = urlsplit(url)
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        matches = [(length, allow) for length, allow, pattern in self.rules if pattern.search(path)]
        return max(matches)[1] if matches else True


class HTTPClient:
    def __init__(self, base_url=BASE_URL, delay=0.4, retries=3):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain"}
        )
        self.delay, self.retries, self.last_request = delay, retries, 0
        self.final_urls = {}
        self.rules = None
        self.document_rules = None
        self.document_robots_checked = False
        self.document_downloaded_bytes = 0
        self.document_unavailable = False
        try:
            self.robots_text = self.get(self.base_url + "/robots.txt", check_robots=False)
        except Exception:
            self.session.close()
            raise
        self.rules = RobotsRules(self.robots_text)
        self.delay = max(self.delay, self.rules.delay)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.session.close()

    def get(self, url, check_robots=True, _redirects=0):
        if _redirects > 5:
            raise SourceError("Слишком много перенаправлений источника")
        if (
            urlsplit(url).netloc != urlsplit(self.base_url).netloc
            or urlsplit(url).scheme != "https"
        ):
            raise SourceError("Переход за пределы публичного HTTPS-источника запрещён")
        if check_robots and self.rules and not self.rules.allowed(url):
            raise SourceError(f"robots.txt запрещает сбор: {url}")
        for attempt in range(self.retries + 1):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            response = None
            try:
                response = self.session.get(
                    url, timeout=(8, 30), allow_redirects=False, stream=True
                )
                if response.status_code in (401, 403):
                    raise SourceError(
                        f"Источник ограничил доступ HTTP {response.status_code}; обход не выполняется"
                    )
                if 300 <= response.status_code < 400:
                    target = urljoin(url, response.headers.get("Location", ""))
                    if target == url:
                        raise SourceError("Цикл перенаправлений источника")
                    result = self.get(target, check_robots=check_robots, _redirects=_redirects + 1)
                    self.final_urls[url] = self.final_urls.get(target, target)
                    return result
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt >= self.retries:
                        raise SourceError(f"Источник недоступен HTTP {response.status_code}")
                    retry_after = response.headers.get("Retry-After", "")
                    pause = float(retry_after) if retry_after.isdigit() else 2**attempt
                    time.sleep(min(max(pause, 1), 30))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 10 * 1024 * 1024:
                        raise SourceError("Ответ источника превышает ограничение 10 MiB")
                    chunks.append(chunk)
                self.final_urls[url] = url
                return b"".join(chunks).decode("utf-8")
            except requests.RequestException as exc:
                if attempt >= self.retries:
                    raise SourceError(f"Ошибка HTTP источника: {type(exc).__name__}") from exc
                time.sleep(min(2**attempt, 8))
            finally:
                if response is not None:
                    response.close()
        raise SourceError("Исчерпаны повторы HTTP")

    def get_booklet_pdf(self, url, expected_size=None):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != BOOKLET_HOST
            or parsed.query
            or not re.fullmatch(r"/assets/[0-9a-fA-F-]{36}\.pdf", parsed.path)
        ):
            raise SourceError("Документ не является безопасной ссылкой на PDF-буклет GloraX")
        if expected_size not in (None, ""):
            try:
                expected_size = int(expected_size)
            except (TypeError, ValueError) as exc:
                raise SourceError("Источник указал некорректный размер PDF") from exc
            if expected_size < 1 or expected_size > MAX_BOOKLET_BYTES:
                raise SourceError(
                    f"Размер буклета превышает ограничение {MAX_BOOKLET_BYTES // (1024 * 1024)} MiB"
                )
        if self.document_downloaded_bytes + (expected_size or 0) > MAX_BOOKLET_BYTES_PER_REFRESH:
            raise SourceError(
                "Достигнут лимит загрузки буклетов на один сбор; оставшиеся PDF перенесены на следующий запуск"
            )
        if self.document_unavailable:
            raise SourceError("Хранилище PDF ранее стало недоступно в этом запуске")
        if not self.document_robots_checked:
            self.document_robots_checked = True
            try:
                response = self.session.get(
                    f"https://{BOOKLET_HOST}/robots.txt",
                    timeout=(4, 8),
                    allow_redirects=False,
                    stream=True,
                )
                try:
                    if response.status_code != 200:
                        self.document_rules = RobotsRules("User-agent: *\nDisallow: /")
                        raise SourceError(
                            f"robots.txt хранилища PDF недоступен (HTTP {response.status_code})"
                        )
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
                raise SourceError(
                    f"Не удалось проверить robots.txt хранилища PDF ({type(exc).__name__})"
                ) from exc
        if not self.document_rules or not self.document_rules.allowed(url):
            raise SourceError("robots.txt запрещает сбор этого PDF")
        response = None
        try:
            response = self.session.get(url, timeout=(6, 20), allow_redirects=False, stream=True)
            if response.status_code in (401, 403):
                raise SourceError(
                    f"Хранилище PDF ограничило доступ HTTP {response.status_code}; обход не выполняется"
                )
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
                LOG.warning(
                    "Размер PDF изменился: url=%s expected=%s received=%s", url, expected_size, size
                )
            self.document_downloaded_bytes += size
            return content
        except requests.RequestException as exc:
            raise SourceError(f"Ошибка загрузки PDF ({type(exc).__name__})") from exc
        finally:
            if response is not None:
                response.close()
