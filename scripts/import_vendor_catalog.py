from __future__ import annotations

import csv
import gzip
import io
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup


SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/1-aq2LZzddN74yHgWKUBFCellpLVYgpTHUXbA_uWw14Y/"
    "export?format=csv&gid=0"
)
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
USER_AGENT = "Mozilla/5.0 (compatible; VendorCatalogImport/1.0; +https://github.com/)"
REQUEST_TIMEOUT = 20
MAX_WORKERS = 32
PROGRESS_EVERY = 50
COMMON_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/post-sitemap.xml",
    "/page-sitemap.xml",
    "/article-sitemap.xml",
    "/news-sitemap.xml",
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


def canonical_site(raw_url: str) -> str:
    cleaned = (raw_url or "").strip()
    parsed = urlparse(cleaned)
    if not parsed.scheme:
        parsed = urlparse(f"https://{cleaned}")
    return urlunparse((parsed.scheme or "https", parsed.netloc, "", "", "", ""))


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


def extract_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for tag in soup.find_all(["a", "link"]):
        href = tag.get("href")
        if not href:
            continue
        lower = href.lower()
        if "sitemap" in lower and lower.startswith("http"):
            candidates.append(href.strip())
    return candidates


def discover_sitemap(session: requests.Session, site_url: str) -> tuple[str, str]:
    site_url = canonical_site(site_url)
    checked: list[str] = []

    robots_url = f"{site_url}/robots.txt"
    robots_response = fetch(session, robots_url)
    if robots_response and robots_response.text:
        for candidate in extract_from_robots(robots_response.text):
            checked.append(candidate)
            response = fetch(session, candidate)
            if response and looks_like_xml_sitemap(response.content, response.url):
                return response.url, "robots.txt"

    for path in COMMON_SITEMAP_PATHS:
        candidate = f"{site_url}{path}"
        checked.append(candidate)
        response = fetch(session, candidate)
        if response and looks_like_xml_sitemap(response.content, response.url):
            return response.url, "common_path"

    homepage = fetch(session, site_url)
    if homepage and "text/html" in homepage.headers.get("content-type", ""):
        for candidate in extract_from_html(homepage.text):
            checked.append(candidate)
            response = fetch(session, candidate)
            if response and looks_like_xml_sitemap(response.content, response.url):
                return response.url, "homepage_link"

    return "", "; ".join(checked[:10])


def download_sheet_rows() -> list[dict[str, str]]:
    response = requests.get(SHEET_CSV_URL, timeout=30)
    response.raise_for_status()
    decoded = response.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(decoded))
    headers = reader.fieldnames or []
    if headers != EXPECTED_HEADERS:
        raise RuntimeError("Google Sheet headers do not match expected columns")
    return list(reader)


def output_catalog_path(base_dir: Path) -> Path:
    return base_dir / "data" / "catalog" / "domains-vendeurs.csv"


def output_missing_path(base_dir: Path) -> Path:
    return base_dir / "data" / "catalog" / "domains-vendeurs-missing-sitemaps.csv"


def main() -> int:
    base_dir = repo_root()
    rows = download_sheet_rows()
    log_info(f"{len(rows)} ligne(s) téléchargée(s) depuis le Google Sheet")

    session = session_with_headers()
    results: list[dict[str, str]] = [dict(row) for row in rows]
    missing_rows: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_index = {
            executor.submit(discover_sitemap, session, row["Site"]): index
            for index, row in enumerate(results)
            if (row.get("Site") or "").strip()
        }
        completed = 0
        total = len(future_to_index)
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            row = results[index]
            site = (row.get("Site") or "").strip()
            sitemap_url = ""
            source = ""
            try:
                sitemap_url, source = future.result()
            except Exception as exc:
                log_warn(f"Échec découverte sitemap pour {site} ({exc})")
            row["Sitemap"] = sitemap_url
            if not sitemap_url:
                missing_rows.append({"Site": site, "checked": source})

            completed += 1
            if completed % PROGRESS_EVERY == 0 or completed == total:
                log_info(f"Découverte des sitemaps: {completed}/{total}")

    session.close()

    catalog_path = output_catalog_path(base_dir)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with catalog_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPECTED_HEADERS)
        writer.writeheader()
        writer.writerows(results)

    missing_path = output_missing_path(base_dir)
    with missing_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Site", "checked"])
        writer.writeheader()
        writer.writerows(sorted(missing_rows, key=lambda item: item["Site"].lower()))

    found = sum(1 for row in results if (row.get("Sitemap") or "").strip())
    log_info(f"{found} sitemap(s) trouvés")
    log_info(f"{len(missing_rows)} site(s) sans sitemap trouvé")
    log_info(f"Catalogue écrit: {catalog_path}")
    log_info(f"Manquants écrits: {missing_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
