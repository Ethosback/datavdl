from __future__ import annotations

import csv
import gzip
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import requests
import tldextract
import trafilatura
from bs4 import BeautifulSoup
from keybert import KeyBERT


EXPECTED_HEADERS = [
    "Site",
    "Visites",
    "Trafic Google",
    "TF",
    "KW",
    "RD",
    "DA",
    "Catégorie",
    "Langue",
    "Taux publication (%)",
    "Délai (j)",
    "Rédaction Ereferer",
    "Soumettre son article",
    "Rédaction par le webmaster",
    "Sitemap",
]

USER_AGENT = "Mozilla/5.0 (compatible; AnalyseVDL/1.0; +https://github.com/)"
SITEMAP_TIMEOUT = int(os.getenv("SITEMAP_TIMEOUT", "30"))
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "20"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
PAGE_WORKERS = int(os.getenv("PAGE_WORKERS", "16"))
PROGRESS_EVERY = int(os.getenv("PROGRESS_EVERY", "25"))
KEYBERT_MODEL = os.getenv("KEYBERT_MODEL", "all-MiniLM-L6-v2")
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
IGNORED_SCHEMES = {"mailto", "tel", "javascript", "data"}
STOPWORDS = {
    "a",
    "an",
    "and",
    "au",
    "aux",
    "avec",
    "ce",
    "ces",
    "comment",
    "dans",
    "de",
    "des",
    "du",
    "en",
    "et",
    "for",
    "guide",
    "how",
    "la",
    "le",
    "les",
    "ou",
    "par",
    "pour",
    "sur",
    "the",
    "to",
    "un",
    "une",
}

TRACKED_CONTENT_SELECTORS = [
    "article",
    "main article",
    "[role='main'] article",
    "[role='main']",
    "main",
    ".post-content",
    ".entry-content",
    ".article-content",
    ".post-body",
    ".entry",
    ".content",
]

REMOVE_FROM_CONTENT = [
    "header",
    "footer",
    "nav",
    "aside",
    "form",
    ".sidebar",
    ".menu",
    ".newsletter",
    ".share",
    ".social",
    ".related",
    ".comments",
    ".footer",
    ".header",
    ".breadcrumbs",
]


@dataclass
class SellerSite:
    site: str
    domain: str
    visits: str
    traffic_google: str
    tf: str
    kw: str
    rd: str
    da: str
    category: str
    language: str
    publication_rate: str
    delay_days: str
    rédaction_ereferer: str
    soumettre_son_article: str
    rédaction_par_le_webmaster: str
    sitemap_url: str


@dataclass
class OutgoingLink:
    target_domain: str
    target_url: str
    anchor_text: str
    rel_flags: list[str]
    is_follow: bool


@dataclass
class PageEvent:
    detected_on: str
    source_domain: str
    source_url: str
    title: str
    keyword: str
    raw_outgoing_links_count: int
    unique_target_domains_count: int


@dataclass
class LinkEvent:
    detected_on: str
    source_domain: str
    source_url: str
    title: str
    keyword: str
    target_domain: str
    target_url: str
    anchor_text: str
    rel_flags: list[str]
    is_follow: bool


def log_info(message: str) -> None:
    print(f"[INFO] {message}", flush=True)


def log_warn(message: str) -> None:
    print(f"[WARN] {message}", flush=True)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


def normalize_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    parts = urlsplit(cleaned)
    if not parts.scheme or not parts.netloc:
        return ""
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def registered_domain(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    host = cleaned
    if "://" in cleaned:
        host = urlsplit(cleaned).netloc
    host = host.split("@")[-1].split(":")[0].strip(".").lower()
    if not host:
        return ""
    extracted = tldextract.extract(host)
    domain = getattr(extracted, "top_domain_under_public_suffix", "")
    if domain:
        return domain.lower()
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}".lower()
    return host.lower()


def session_with_headers() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch_response(
    session: requests.Session,
    url: str,
    timeout: int,
) -> requests.Response:
    response = session.get(url, timeout=timeout, allow_redirects=True)
    if response.status_code >= 400:
        response.raise_for_status()
    return response


def get_with_retries(session: requests.Session, url: str, timeout: int) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            return fetch_response(session, url, timeout)
        except requests.HTTPError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code not in RETRYABLE_STATUS_CODES or attempt == HTTP_RETRIES:
                raise
        except requests.RequestException as exc:
            last_error = exc
            if attempt == HTTP_RETRIES:
                raise
        time.sleep(min(attempt, 3))
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"unexpected retry state for {url}")


def maybe_decompress(content: bytes, url: str) -> bytes:
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except OSError:
            return content
    return content


def local_name(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def catalog_path(base_dir: Path) -> Path:
    return base_dir / "data" / "catalog" / "domains-vendeurs.csv"


def snapshot_path(base_dir: Path, site: SellerSite) -> Path:
    return base_dir / "data" / "state" / "snapshots" / f"{slugify(site.domain)}.json"


def ever_seen_path(base_dir: Path, site: SellerSite) -> Path:
    return base_dir / "data" / "state" / "ever_seen" / f"{slugify(site.domain)}.json"


def page_events_path(base_dir: Path, day: str) -> Path:
    return base_dir / "data" / "events" / "pages" / f"{day}.jsonl.gz"


def link_events_path(base_dir: Path, day: str) -> Path:
    return base_dir / "data" / "events" / "links" / f"{day}.jsonl.gz"


def load_url_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return set(payload.get("urls", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_url_set(path: Path, urls: Iterable[str], sitemap_url: str | None = None) -> None:
    ensure_dir(path.parent)
    payload: dict[str, object] = {
        "updated_on": date.today().isoformat(),
        "urls": sorted(set(urls)),
    }
    if sitemap_url:
        payload["sitemap_url"] = sitemap_url
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl_gz(path: Path, rows: Iterable[dict]) -> None:
    ensure_dir(path.parent)
    with gzip.open(path, "at", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_sellers(base_dir: Path) -> list[SellerSite]:
    input_path = catalog_path(base_dir)
    if not input_path.exists():
        raise FileNotFoundError(f"missing catalogue file: {input_path}")

    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if headers != EXPECTED_HEADERS:
            raise RuntimeError(
                "domains-vendeurs.csv must contain exactly these headers, in this order:\n"
                + ", ".join(EXPECTED_HEADERS)
            )

        sellers: list[SellerSite] = []
        for row in reader:
            site_value = (row["Site"] or "").strip()
            sitemap_value = (row["Sitemap"] or "").strip()
            if not site_value or not sitemap_value:
                continue
            sellers.append(
                SellerSite(
                    site=site_value,
                    domain=registered_domain(site_value),
                    visits=(row["Visites"] or "").strip(),
                    traffic_google=(row["Trafic Google"] or "").strip(),
                    tf=(row["TF"] or "").strip(),
                    kw=(row["KW"] or "").strip(),
                    rd=(row["RD"] or "").strip(),
                    da=(row["DA"] or "").strip(),
                    category=(row["Catégorie"] or "").strip(),
                    language=(row["Langue"] or "").strip(),
                    publication_rate=(row["Taux publication (%)"] or "").strip(),
                    delay_days=(row["Délai (j)"] or "").strip(),
                    rédaction_ereferer=(row["Rédaction Ereferer"] or "").strip(),
                    soumettre_son_article=(row["Soumettre son article"] or "").strip(),
                    rédaction_par_le_webmaster=(row["Rédaction par le webmaster"] or "").strip(),
                    sitemap_url=sitemap_value,
                )
            )
    return sellers


def parse_sitemap(
    session: requests.Session,
    sitemap_url: str,
    visited: set[str] | None = None,
) -> tuple[set[str], bool]:
    normalized_sitemap_url = normalize_url(sitemap_url)
    if not normalized_sitemap_url:
        return set(), False
    if visited is None:
        visited = set()
    if normalized_sitemap_url in visited:
        return set(), True
    visited.add(normalized_sitemap_url)

    try:
        response = get_with_retries(session, normalized_sitemap_url, SITEMAP_TIMEOUT)
        raw_bytes = response.content
        final_url = response.url
    except requests.RequestException as exc:
        log_warn(f"sitemap inaccessible: {normalized_sitemap_url} ({exc})")
        return set(), False

    xml_bytes = maybe_decompress(raw_bytes, final_url)
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log_warn(f"XML invalide: {normalized_sitemap_url} ({exc})")
        return set(), False

    root_name = local_name(root.tag)
    if root_name == "urlset":
        urls: set[str] = set()
        for url_node in root:
            if local_name(url_node.tag) != "url":
                continue
            for child in url_node:
                if local_name(child.tag) == "loc" and child.text:
                    normalized = normalize_url(child.text)
                    if normalized:
                        urls.add(normalized)
        return urls, True

    if root_name == "sitemapindex":
        urls: set[str] = set()
        all_ok = True
        for sitemap_node in root:
            if local_name(sitemap_node.tag) != "sitemap":
                continue
            child_url = ""
            for child in sitemap_node:
                if local_name(child.tag) == "loc" and child.text:
                    child_url = child.text.strip()
                    break
            if not child_url:
                continue
            child_urls, child_ok = parse_sitemap(session, child_url, visited)
            urls.update(child_urls)
            if not child_ok:
                all_ok = False
        return urls, all_ok

    log_warn(f"format XML non géré: {normalized_sitemap_url}")
    return set(), False


def focus_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", title).strip()
    if not cleaned:
        return ""
    parts = [part.strip() for part in re.split(r"\s(?:\||-|–|:|»)\s", cleaned) if part.strip()]
    return parts[0] if parts else cleaned


def extract_keyword_from_title(extractor: KeyBERT, title: str) -> str:
    base_text = focus_title(title)
    if not base_text:
        return ""
    for ngram_range in ((2, 3), (1, 2), (1, 1)):
        keywords = extractor.extract_keywords(
            base_text,
            keyphrase_ngram_range=ngram_range,
            stop_words=list(STOPWORDS),
            top_n=5,
        )
        for phrase, _score in keywords:
            phrase = phrase.strip()
            if phrase:
                return phrase
    return ""


def anchor_rel_flags(anchor) -> list[str]:
    values = anchor.get("rel", [])
    if isinstance(values, str):
        values = values.split()
    return sorted({value.strip().lower() for value in values if value.strip()})


def extract_links_from_fragment(fragment_html: str, base_url: str, source_domain: str) -> list[OutgoingLink]:
    soup = BeautifulSoup(fragment_html, "html.parser")
    return extract_links_from_container(soup, base_url, source_domain)


def extract_links_from_container(container, base_url: str, source_domain: str) -> list[OutgoingLink]:
    results: list[OutgoingLink] = []
    source_registered = registered_domain(source_domain)

    for anchor in container.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme.lower() in IGNORED_SCHEMES:
            continue
        if not parts.scheme or not parts.netloc:
            continue
        normalized_target = normalize_url(absolute)
        if not normalized_target:
            continue

        target_domain = registered_domain(normalized_target)
        if not target_domain:
            continue
        if target_domain == source_registered:
            continue

        rel_flags = anchor_rel_flags(anchor)
        results.append(
            OutgoingLink(
                target_domain=target_domain,
                target_url=normalized_target,
                anchor_text=re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip(),
                rel_flags=rel_flags,
                is_follow="nofollow" not in rel_flags,
            )
        )
    return results


def choose_fallback_content_node(soup: BeautifulSoup):
    candidates = []
    for selector in TRACKED_CONTENT_SELECTORS:
        for node in soup.select(selector):
            text_length = len(node.get_text(" ", strip=True))
            if text_length:
                candidates.append((text_length, node))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    return soup.body or soup


def extract_main_content_links(html: str, page_url: str, source_domain: str) -> list[OutgoingLink]:
    try:
        extracted_html = trafilatura.extract(
            html,
            url=page_url,
            output_format="html",
            include_links=True,
            include_images=False,
            include_tables=False,
            favor_precision=True,
        )
    except Exception:
        extracted_html = None

    if extracted_html:
        links = extract_links_from_fragment(extracted_html, page_url, source_domain)
        if links:
            return links

    soup = BeautifulSoup(html, "html.parser")
    content_node = choose_fallback_content_node(soup)
    for selector in REMOVE_FROM_CONTENT:
        for node in content_node.select(selector):
            node.decompose()
    return extract_links_from_container(content_node, page_url, source_domain)


def fetch_page_html_and_title(url: str) -> tuple[str, str]:
    session = session_with_headers()
    response: requests.Response | None = None
    try:
        response = get_with_retries(session, url, PAGE_TIMEOUT)
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        title = ""
        if soup.title and soup.title.get_text():
            title = re.sub(r"\s+", " ", soup.title.get_text(" ", strip=True)).strip()
        return html, title
    except requests.RequestException:
        return "", ""
    finally:
        if response is not None:
            response.close()
        session.close()


def write_json(path: Path, payload) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def process() -> int:
    base_dir = repo_root()
    today = date.today().isoformat()
    sellers = read_sellers(base_dir)
    if not sellers:
        log_info("Aucun site vendeur trouvé dans domains-vendeurs.csv")
        return 0

    session = session_with_headers()
    pending_urls: list[tuple[SellerSite, str]] = []
    total_sites = len(sellers)

    for index, site in enumerate(sellers, start=1):
        log_info(f"[{index}/{total_sites}] Traitement {site.domain}")
        current_urls, crawl_complete = parse_sitemap(session, site.sitemap_url)
        if not crawl_complete:
            log_warn(f"Crawl sitemap incomplet pour {site.domain}, snapshot conservé.")
            continue
        if not current_urls:
            log_info(f"Aucune URL récupérée pour {site.domain}, snapshot conservé.")
            continue

        snapshot_file = snapshot_path(base_dir, site)
        ever_seen_file = ever_seen_path(base_dir, site)
        site_is_new = not snapshot_file.exists()
        previous_urls = load_url_set(snapshot_file)
        ever_seen_urls = load_url_set(ever_seen_file)

        save_url_set(snapshot_file, current_urls, sitemap_url=site.sitemap_url)
        updated_ever_seen = set(ever_seen_urls)
        updated_ever_seen.update(current_urls)
        save_url_set(ever_seen_file, updated_ever_seen, sitemap_url=site.sitemap_url)

        if site_is_new:
            log_info(
                f"Initialisation silencieuse pour {site.domain}: "
                f"{len(current_urls)} URL(s) enregistrée(s)"
            )
            continue

        new_urls = sorted(url for url in current_urls - previous_urls if url not in ever_seen_urls)
        if not new_urls:
            continue

        log_info(f"{site.domain}: {len(new_urls)} nouvelle(s) URL(s)")
        for url in new_urls:
            pending_urls.append((site, url))

    session.close()

    if not pending_urls:
        log_info("Aucune nouvelle URL à enrichir.")
        return 0

    title_results: dict[str, tuple[str, str]] = {}
    log_info(f"{len(pending_urls)} nouvelle(s) URL(s) à télécharger")
    with ThreadPoolExecutor(max_workers=max(1, PAGE_WORKERS)) as executor:
        future_to_url = {executor.submit(fetch_page_html_and_title, url): url for _site, url in pending_urls}
        completed = 0
        total = len(future_to_url)
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                title_results[url] = future.result()
            except Exception as exc:
                log_warn(f"échec inattendu sur {url} ({exc})")
                title_results[url] = ("", "")
            completed += 1
            if completed % PROGRESS_EVERY == 0 or completed == total:
                log_info(f"Pages téléchargées: {completed}/{total}")

    log_info(f"Initialisation KeyBERT ({KEYBERT_MODEL})")
    extractor = KeyBERT(model=KEYBERT_MODEL)
    page_events: list[PageEvent] = []
    link_events: list[LinkEvent] = []

    for site, url in pending_urls:
        html, title = title_results.get(url, ("", ""))
        keyword = extract_keyword_from_title(extractor, title) if title else ""
        outgoing_links = extract_main_content_links(html, url, site.domain) if html else []
        unique_targets = {link.target_domain for link in outgoing_links}

        page_events.append(
            PageEvent(
                detected_on=today,
                source_domain=site.domain,
                source_url=url,
                title=title,
                keyword=keyword,
                raw_outgoing_links_count=len(outgoing_links),
                unique_target_domains_count=len(unique_targets),
            )
        )

        for link in outgoing_links:
            link_events.append(
                LinkEvent(
                    detected_on=today,
                    source_domain=site.domain,
                    source_url=url,
                    title=title,
                    keyword=keyword,
                    target_domain=link.target_domain,
                    target_url=link.target_url,
                    anchor_text=link.anchor_text,
                    rel_flags=link.rel_flags,
                    is_follow=link.is_follow,
                )
            )

    append_jsonl_gz(page_events_path(base_dir, today), (asdict(event) for event in page_events))
    append_jsonl_gz(link_events_path(base_dir, today), (asdict(event) for event in link_events))
    log_info(
        f"{len(page_events)} page event(s) et {len(link_events)} link event(s) écrits pour {today}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(process())
