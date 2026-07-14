import base64
import hashlib
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from lead_finder import Lead, SOCIAL_HOSTS, USER_AGENT
from storage import LeadStore


YANDEX_SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
EXCLUDED_HOSTS = (
    "yandex.ru",
    "2gis.ru",
    "google.com",
    "vk.com",
    "t.me",
    "telegram.me",
    "instagram.com",
    "facebook.com",
    "ok.ru",
    "zoon.ru",
    "yell.ru",
    "flamp.ru",
    "orgpage.ru",
    "prodoctorov.ru",
    "avito.ru",
)
NAME_STOPWORDS = {
    "ооо",
    "ип",
    "компания",
    "клиника",
    "салон",
    "центр",
    "студия",
    "стоматология",
}
CONTACT_WORDS = ("contact", "contacts", "kontakty", "контакт", "o-kompanii", "about")
BOOKING_WORDS = ("booking", "appointment", "запис", "yclients", "dikidi")
CACHEABLE_STATUSES = {"site_found", "likely_no_site"}


@dataclass
class VerificationResult:
    status: str
    website: str = ""
    evidence: list[str] | None = None
    api_requests: int = 0
    from_cache: bool = False

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = []


class _ContactParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.has_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag.lower() == "a" and attributes.get("href"):
            self.links.append(attributes["href"].strip())
        if tag.lower() == "form":
            self.has_form = True


def calculate_request_allowance(
    monthly_budget: float,
    price_per_1000: float,
    used_requests: int,
    requested: int,
) -> int:
    if monthly_budget <= 0 or price_per_1000 <= 0 or requested <= 0:
        return 0
    monthly_limit = math.floor(monthly_budget * 1000 / price_per_1000)
    return min(requested, max(0, monthly_limit - used_requests))


def estimated_cost(requests_count: int, price_per_1000: float) -> float:
    return round(max(0, requests_count) * max(0, price_per_1000) / 1000, 3)


def manual_yandex_search_url(lead: Lead) -> str:
    return "https://yandex.ru/search/?text=" + quote_plus(
        " ".join(part for part in (f'"{lead.name}"', lead.city, lead.phone) if part)
    )


def parse_yandex_xml(xml: str) -> list[str]:
    root = ET.fromstring(xml)
    return [node.text.strip() for node in root.findall(".//doc/url") if node.text and node.text.strip()]


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().split(":", 1)[0].removeprefix("www.")


def is_excluded_result(url: str) -> bool:
    host = _host(url)
    return not host or any(host == item or host.endswith(f".{item}") for item in EXCLUDED_HOSTS)


def _phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else ""


def _normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[a-zа-яё0-9]+", (value or "").lower()))


def domain_verification_key(lead: Lead) -> str:
    phone = _phone_key(lead.phone)
    parts = [_normalized_text(lead.name), _normalized_text(lead.city), phone]
    if not phone:
        parts.append(_normalized_text(lead.address))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def candidate_matches(lead: Lead, html: str) -> tuple[bool, str]:
    phone = _phone_key(lead.phone)
    if phone and phone in re.sub(r"\D", "", html or ""):
        return True, "на странице совпал телефон компании"

    page = _normalized_text(html)
    words = [
        word
        for word in _normalized_text(lead.name).split()
        if len(word) >= 4 and word not in NAME_STOPWORDS
    ]
    matched_words = [word for word in words if word in page]
    required = 1 if len(words) == 1 else 2
    street = (lead.address.split(",", 1)[0] if lead.address else "").strip()
    locations = [_normalized_text(value) for value in (lead.city, street) if value.strip()]
    if len(matched_words) >= required and any(location and location in page for location in locations):
        return True, "на странице совпали название и город/улица"
    return False, ""


def _read_page(
    session: requests.Session,
    url: str,
    timeout: int = 7,
    max_bytes: int = 200 * 1024,
) -> tuple[str, str] | None:
    response = session.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        allow_redirects=True,
        stream=True,
    )
    if response.status_code >= 400:
        return None
    final_url = getattr(response, "url", url) or url
    if hasattr(response, "iter_content"):
        data = bytearray()
        for chunk in response.iter_content(8192):
            data.extend(chunk)
            if len(data) > max_bytes:
                return None
        encoding = getattr(response, "encoding", None) or "utf-8"
        return bytes(data).decode(encoding, errors="replace"), final_url
    text = getattr(response, "text", "") or ""
    if len(text.encode("utf-8")) > max_bytes:
        return None
    return text, final_url


def _decode_search_response(response: requests.Response) -> str:
    raw_data = response.json().get("rawData", "")
    if not raw_data:
        raise ValueError("Yandex Search API вернул пустой ответ.")
    try:
        return base64.b64decode(raw_data).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return raw_data


def _search_yandex(
    query_text: str,
    api_key: str,
    folder_id: str,
    groups: int,
    session: requests.Session | None = None,
) -> list[str]:
    client = session or requests
    payload = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query_text,
            "familyMode": "FAMILY_MODE_STRICT",
            "page": "0",
            "fixTypoMode": "FIX_TYPO_MODE_ON",
        },
        "groupSpec": {
            "groupsOnPage": str(groups),
            "docsInGroup": "1",
            "groupMode": "GROUP_MODE_DEEP",
        },
        "region": "225",
        "l10N": "LOCALIZATION_RU",
        "folderId": folder_id,
        "responseFormat": "FORMAT_XML",
    }
    response = client.post(
        YANDEX_SEARCH_URL,
        json=payload,
        headers={"Authorization": f"Api-Key {api_key}", "User-Agent": USER_AGENT},
        timeout=15,
    )
    response.raise_for_status()
    return parse_yandex_xml(_decode_search_response(response))


def check_yandex_connection(
    api_key: str,
    folder_id: str,
    session: requests.Session | None = None,
) -> tuple[bool, str, int]:
    if not api_key or not folder_id:
        return False, "Yandex Search API не настроен.", 0
    try:
        _search_yandex("Екатеринбург", api_key, folder_id, 1, session)
        return True, "Подключение работает: Yandex Search API вернул ответ.", 1
    except requests.HTTPError as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        suffix = f" HTTP {status}." if status else "."
        return False, "Yandex Search API отклонил запрос:" + suffix, 1
    except requests.Timeout:
        return False, "Yandex Search API не ответил вовремя.", 1
    except (requests.RequestException, ValueError, ET.ParseError):
        return False, "Не удалось подключиться к Yandex Search API.", 1


def verify_lead_site(
    lead: Lead,
    api_key: str,
    folder_id: str,
    session: requests.Session | None = None,
) -> VerificationResult:
    client = session or requests
    query = " ".join(part for part in (f'"{lead.name}"', lead.city, lead.phone) if part)
    try:
        urls = [
            url
            for url in _search_yandex(query, api_key, folder_id, 5, client)
            if not is_excluded_result(url)
        ][:3]
        matches: list[tuple[str, str]] = []
        unverified_candidates = 0
        for url in urls:
            try:
                page = _read_page(client, url)
            except requests.RequestException:
                unverified_candidates += 1
                continue
            if not page:
                unverified_candidates += 1
                continue
            html, final_url = page
            matched, evidence = candidate_matches(lead, html)
            if matched and not is_excluded_result(final_url):
                root_url = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"
                if all(_host(saved_url) != _host(root_url) for saved_url, _ in matches):
                    matches.append((root_url, evidence))
        if len(matches) == 1:
            website, evidence = matches[0]
            return VerificationResult("site_found", website, [evidence, f"домен найден через Yandex Search API: {_host(website)}"], 1)
        if len(matches) > 1:
            return VerificationResult(
                "ambiguous",
                evidence=["несколько доменов прошли проверку: " + ", ".join(_host(url) for url, _ in matches)],
                api_requests=1,
            )
        if unverified_candidates:
            return VerificationResult(
                "ambiguous",
                evidence=["часть найденных доменов недоступна для проверки"],
                api_requests=1,
            )
        return VerificationResult("likely_no_site", evidence=["официальный сайт не найден среди проверенных результатов"], api_requests=1)
    except (requests.RequestException, ValueError, ET.ParseError) as error:
        return VerificationResult("verification_error", evidence=[f"ошибка проверки: {error}"], api_requests=1)


def _cached_verification(lead: Lead, store: LeadStore) -> VerificationResult | None:
    cached = store.get_domain_verification(domain_verification_key(lead))
    if not cached:
        return None
    return VerificationResult(
        status=str(cached["status"]),
        website=str(cached["website"]),
        evidence=list(cached["evidence"]),
        from_cache=True,
    )


def verify_lead_site_cached(
    lead: Lead,
    api_key: str,
    folder_id: str,
    store: LeadStore,
    force_refresh: bool = False,
    session: requests.Session | None = None,
) -> VerificationResult:
    if not force_refresh:
        cached = _cached_verification(lead, store)
        if cached:
            return cached
    result = verify_lead_site(lead, api_key, folder_id, session)
    if result.status in CACHEABLE_STATUSES:
        store.save_domain_verification(
            domain_verification_key(lead),
            result.status,
            result.website,
            list(result.evidence or []),
        )
    return result


def verify_missing_leads(
    leads: list[Lead],
    api_key: str,
    folder_id: str,
    max_requests: int,
    dry_run: bool = False,
    session: requests.Session | None = None,
    store: LeadStore | None = None,
) -> tuple[list[Lead], dict[str, int]]:
    stats = {"yandex_checked": 0, "sites_found": 0, "api_requests": 0, "cache_hits": 0}
    if dry_run:
        return leads, stats

    remaining = max(0, max_requests)
    for lead in leads:
        broken_osm_site = bool(
            lead.website
            and lead.website_source == "OpenStreetMap"
            and lead.audit is not None
            and lead.audit.state == "broken"
        )
        if lead.website and not broken_osm_site:
            continue
        result = _cached_verification(lead, store) if store else None
        if result:
            stats["cache_hits"] += 1
        elif not api_key or not folder_id:
            if broken_osm_site:
                lead.verification_evidence.append(
                    "актуальный домен не проверен: Yandex Search API не настроен"
                )
            else:
                lead.verification_status = "ambiguous"
                lead.verification_evidence = ["Yandex Search API не настроен; нужна ручная проверка"]
            continue
        if result is None and remaining <= 0:
            if broken_osm_site:
                lead.verification_evidence.append(
                    "актуальный домен не проверен: месячный лимит Yandex Search API исчерпан"
                )
            else:
                lead.verification_status = "ambiguous"
                lead.verification_evidence = ["месячный лимит Yandex Search API исчерпан"]
            continue
        previous_website = lead.website
        previous_source = lead.website_source
        previous_status = lead.verification_status
        previous_audit = lead.audit
        if result is None:
            result = (
                verify_lead_site_cached(
                    lead,
                    api_key,
                    folder_id,
                    store,
                    force_refresh=True,
                    session=session,
                )
                if store
                else verify_lead_site(lead, api_key, folder_id, session)
            )
        remaining -= result.api_requests
        stats["api_requests"] += result.api_requests
        stats["yandex_checked"] += result.api_requests
        if broken_osm_site:
            if result.website and _host(result.website) != _host(previous_website):
                lead.verification_status = "site_found"
                lead.verification_evidence.extend(
                    [f"старый сайт из OpenStreetMap: {previous_website}", *(result.evidence or [])]
                )
                lead.website = result.website
                lead.website_source = "Yandex Search API"
                lead.audit = None
                stats["sites_found"] += 1
            else:
                lead.website = previous_website
                lead.website_source = previous_source
                lead.verification_status = previous_status
                lead.audit = previous_audit
                lead.verification_evidence.extend(result.evidence or [])
            continue

        lead.verification_status = result.status
        lead.verification_evidence = list(result.evidence or [])
        if result.website:
            lead.website = result.website
            lead.website_source = "Yandex Search API"
            lead.audit = None
            stats["sites_found"] += 1
    return leads, stats


def _same_domain(base: str, url: str) -> bool:
    return _host(base) == _host(url)


def crawl_contacts(
    website: str,
    session: requests.Session | None = None,
    timeout: int = 7,
) -> dict[str, str | bool]:
    client = session or requests
    base = f"{urlparse(website).scheme}://{urlparse(website).netloc}"
    robots = RobotFileParser()
    robots.set_url(urljoin(base, "/robots.txt"))
    try:
        robots_page = _read_page(client, robots.url, timeout)
        robots.parse(robots_page[0].splitlines() if robots_page else [])
    except requests.RequestException:
        robots.parse([])

    result: dict[str, str | bool] = {
        "phone": "",
        "email": "",
        "social": "",
        "has_form": False,
        "contact_page": "",
        "online_booking": False,
    }
    queue = [website]
    visited: set[str] = set()
    while queue and len(visited) < 3:
        url = queue.pop(0)
        if url in visited or not _same_domain(base, url) or not robots.can_fetch(USER_AGENT, url):
            continue
        visited.add(url)
        try:
            page = _read_page(client, url, timeout)
        except requests.RequestException:
            continue
        if not page:
            continue
        html, final_url = page
        parser = _ContactParser()
        parser.feed(html)
        result["has_form"] = bool(result["has_form"] or parser.has_form)
        for href in parser.links:
            absolute = urljoin(final_url, href)
            lowered = href.lower()
            if href.lower().startswith("tel:") and not result["phone"]:
                result["phone"] = href[4:].strip()
            elif href.lower().startswith("mailto:") and not result["email"]:
                result["email"] = href[7:].split("?", 1)[0].strip()
            elif any(_host(absolute) == host or _host(absolute).endswith(f".{host}") for host in SOCIAL_HOSTS):
                if not result["social"]:
                    result["social"] = absolute
            if any(word in lowered for word in BOOKING_WORDS):
                result["online_booking"] = True
            if (
                any(word in lowered for word in CONTACT_WORDS)
                and _same_domain(base, absolute)
                and absolute not in visited
                and absolute not in queue
                and len(queue) < 2
            ):
                queue.append(absolute)
                if not result["contact_page"]:
                    result["contact_page"] = absolute
    return result
