from __future__ import annotations

import csv
import gzip
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup


USER_AGENT = "Mozilla/5.0 (compatible; SitemapSecondPass/1.0; +https://github.com/)"
REQUEST_TIMEOUT = 20
MAX_WORKERS = 24
PROGRESS_EVERY = 25
AGGRESSIVE_PATHS = [
    "/sitemap.xml",
    "/sitemap.xml.gz",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemapindex.xml",
    "/wp-sitemap.xml",
    "/sitemaps.xml",
    "/sitemap.php",
    "/sitemap/sitemap.xml",
    "/sitemap/index.xml",
    "/index.php/sitemap.xml",
    "/index.php/sitemap.xml.gz",
    "/index.php/sitemap_index.xml",
    "/index.php/sitemap-index.xml",
    "/index.php/sitemapindex.xml",
    "/post-sitemap.xml",
    "/page-sitemap.xml",
    "/article-sitemap.xml",
    "/news-sitemap.xml",
    "/product-sitemap.xml",
    "/category-sitemap.xml",
    "/sitemap1.xml",
]


def log_info(message: str) -> None:
    print(f"[INFO] {message}", flush=True)


def log_warn(message: str) -> None:
    print(f"[WARN] {message}", flush=True)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def session_with_headers() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def maybe_decompress(content: bytes, url: str) -> bytes:
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except OSError:
            return content
    return content


def local_name(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def looks_like_xml_sitemap(content: bytes, url: str) -> bool:
    payload = maybe_decompress(content, url)
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return False
    return local_name(root.tag) in {"urlset", "sitemapindex"}


def fetch(session: requests.Session, url: str) -> requests.Response | None:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if response.status_code >= 400:
            return None
        return response
    except requests.RequestException:
        return None


def extract_from_robots(robots_text: str) -> list[str]:
    matches = []
    for line in robots_text.splitlines():
        match = re.match(r"(?i)\s*sitemap\s*:\s*(\S+)", line.strip())
        if match:
            matches.append(match.group(1).strip())
    return matches


def extract_from_html(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for tag in soup.find_all(["a", "link"]):
        href = tag.get("href")
        if not href:
            continue
        lower = href.lower()
        if "sitemap" not in lower:
            continue
        absolute = urljoin(base_url, href.strip())
        candidates.append(absolute)
    return candidates


def normalize_base(site: str) -> tuple[str, str, str]:
    parsed = urlparse(site.strip())
    if not parsed.scheme:
        parsed = urlparse(f"https://{site.strip()}")
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    path = parsed.path.rstrip("/")
    origin = urlunparse((scheme, netloc, "", "", "", ""))
    page = urlunparse((scheme, netloc, path or "", "", "", ""))
    return scheme, netloc, page or origin


def base_variants(site: str) -> list[str]:
    scheme, netloc, page = normalize_base(site)
    host = netloc.lower()
    variants: list[str] = []

    hosts = {host}
    if host.startswith("www."):
        hosts.add(host[4:])
    else:
        hosts.add(f"www.{host}")

    schemes = {scheme, "https", "http"}
    paths = {urlparse(page).path.rstrip("/"), ""}

    for candidate_scheme in schemes:
        for candidate_host in hosts:
            for candidate_path in paths:
                variants.append(
                    urlunparse((candidate_scheme, candidate_host, candidate_path, "", "", ""))
                )

    deduped = []
    seen = set()
    for value in variants:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def discover_sitemap_aggressive(session: requests.Session, site_url: str) -> tuple[str, str]:
    checked: list[str] = []

    for base in base_variants(site_url):
        robots_url = f"{base}/robots.txt"
        checked.append(robots_url)
        robots_response = fetch(session, robots_url)
        if robots_response and robots_response.text:
            for candidate in extract_from_robots(robots_response.text):
                checked.append(candidate)
                response = fetch(session, candidate)
                if response and looks_like_xml_sitemap(response.content, response.url):
                    return response.url, "robots.txt"

    for base in base_variants(site_url):
        for path in AGGRESSIVE_PATHS:
            candidate = f"{base}{path}"
            checked.append(candidate)
            response = fetch(session, candidate)
            if response and looks_like_xml_sitemap(response.content, response.url):
                return response.url, "common_path"

    for base in base_variants(site_url):
        homepage = fetch(session, base)
        if homepage and "text/html" in homepage.headers.get("content-type", ""):
            for candidate in extract_from_html(homepage.text, homepage.url):
                checked.append(candidate)
                response = fetch(session, candidate)
                if response and looks_like_xml_sitemap(response.content, response.url):
                    return response.url, "homepage_link"

    return "", "; ".join(checked[:20])


def load_missing_sites(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_catalog_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        return headers, list(reader)


def save_catalog_rows(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def save_missing_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Site", "checked"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    base_dir = repo_root()
    catalog_path = base_dir / "data" / "catalog" / "domains-vendeurs.csv"
    missing_path = base_dir / "data" / "catalog" / "domains-vendeurs-missing-sitemaps.csv"

    headers, catalog_rows = load_catalog_rows(catalog_path)
    missing_rows = load_missing_sites(missing_path)
    if not missing_rows:
        log_info("Aucun site manquant à retraiter.")
        return 0

    row_by_site = {(row.get("Site") or "").strip(): row for row in catalog_rows}
    unresolved: list[dict[str, str]] = []
    found_count = 0
    session = session_with_headers()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_site = {
            executor.submit(discover_sitemap_aggressive, session, row["Site"]): row["Site"]
            for row in missing_rows
            if (row.get("Site") or "").strip()
        }
        completed = 0
        total = len(future_to_site)
        for future in as_completed(future_to_site):
            site = future_to_site[future]
            sitemap_url = ""
            checked = ""
            try:
                sitemap_url, checked = future.result()
            except Exception as exc:
                checked = str(exc)
                log_warn(f"Échec second passage pour {site} ({exc})")

            row = row_by_site.get(site)
            if row is not None and sitemap_url:
                row["Sitemap"] = sitemap_url
                found_count += 1
            else:
                unresolved.append({"Site": site, "checked": checked})

            completed += 1
            if completed % PROGRESS_EVERY == 0 or completed == total:
                log_info(f"Second passage des sitemaps: {completed}/{total}")

    session.close()

    save_catalog_rows(catalog_path, headers, catalog_rows)
    unresolved.sort(key=lambda item: item["Site"].lower())
    save_missing_rows(missing_path, unresolved)

    log_info(f"{found_count} sitemap(s) supplémentaires trouvés")
    log_info(f"{len(unresolved)} site(s) restent sans sitemap trouvé")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
