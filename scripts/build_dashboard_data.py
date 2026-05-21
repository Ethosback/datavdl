from __future__ import annotations

import csv
import gzip
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import tldextract


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


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def catalog_path(base_dir: Path) -> Path:
    return base_dir / "data" / "catalog" / "domains-vendeurs.csv"


def registered_domain(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    host = cleaned
    if "://" in cleaned:
        host = urlsplit(cleaned).netloc
    host = host.split("@")[-1].split(":")[0].strip(".").lower()
    extracted = tldextract.extract(host)
    domain = getattr(extracted, "top_domain_under_public_suffix", "")
    if domain:
        return domain.lower()
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}".lower()
    return host.lower()


def load_catalog(base_dir: Path) -> dict[str, dict[str, str]]:
    path = catalog_path(base_dir)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if (reader.fieldnames or []) != EXPECTED_HEADERS:
            raise RuntimeError("invalid catalogue headers")
        catalog: dict[str, dict[str, str]] = {}
        for row in reader:
            domain = registered_domain((row["Site"] or "").strip())
            if not domain:
                continue
            catalog[domain] = row
        return catalog


def load_jsonl_gz(path: Path) -> list[dict]:
    rows: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_json(path: Path, payload) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build() -> int:
    base_dir = repo_root()
    catalog = load_catalog(base_dir)

    page_dir = base_dir / "data" / "events" / "pages"
    link_dir = base_dir / "data" / "events" / "links"
    page_events: list[dict] = []
    link_events: list[dict] = []

    for path in sorted(page_dir.glob("*.jsonl.gz")):
        page_events.extend(load_jsonl_gz(path))
    for path in sorted(link_dir.glob("*.jsonl.gz")):
        link_events.extend(load_jsonl_gz(path))

    pages_by_domain: dict[str, list[dict]] = defaultdict(list)
    links_by_source: dict[str, list[dict]] = defaultdict(list)
    links_by_target: dict[str, list[dict]] = defaultdict(list)
    edge_stats: dict[tuple[str, str], dict[str, object]] = {}

    for event in page_events:
        pages_by_domain[event["source_domain"]].append(event)

    for event in link_events:
        source = event["source_domain"]
        target = event["target_domain"]
        links_by_source[source].append(event)
        links_by_target[target].append(event)

        key = (source, target)
        edge = edge_stats.setdefault(
            key,
            {
                "source_domain": source,
                "target_domain": target,
                "links_count": 0,
                "source_urls": set(),
                "first_seen": event["detected_on"],
                "last_seen": event["detected_on"],
            },
        )
        edge["links_count"] += 1
        edge["source_urls"].add(event["source_url"])
        if event["detected_on"] < edge["first_seen"]:
            edge["first_seen"] = event["detected_on"]
        if event["detected_on"] > edge["last_seen"]:
            edge["last_seen"] = event["detected_on"]

    sellers_summary: list[dict] = []
    for source_domain in sorted(catalog):
        page_list = pages_by_domain.get(source_domain, [])
        link_list = links_by_source.get(source_domain, [])
        unique_targets = sorted({link["target_domain"] for link in link_list})
        catalog_row = catalog.get(source_domain, {})
        articles_with_links = sum(1 for event in page_list if event["raw_outgoing_links_count"] > 0)
        raw_links = sum(event["raw_outgoing_links_count"] for event in page_list)
        unique_targets_total = sum(event["unique_target_domains_count"] for event in page_list)
        sellers_summary.append(
            {
                "domain": source_domain,
                "articles_analyzed": len(page_list),
                "articles_with_external_links": articles_with_links,
                "raw_outgoing_links_count": raw_links,
                "unique_target_domains_count": len(unique_targets),
                "avg_raw_links_per_article": round(raw_links / len(page_list), 2) if page_list else 0,
                "avg_unique_target_domains_per_article": round(
                    unique_targets_total / len(page_list), 2
                )
                if page_list
                else 0,
                "Visites": catalog_row.get("Visites", ""),
                "Trafic Google": catalog_row.get("Trafic Google", ""),
                "TF": catalog_row.get("TF", ""),
                "KW": catalog_row.get("KW", ""),
                "RD": catalog_row.get("RD", ""),
                "DA": catalog_row.get("DA", ""),
                "Catégorie": catalog_row.get("Catégorie", ""),
                "Langue": catalog_row.get("Langue", ""),
                "Taux publication (%)": catalog_row.get("Taux publication (%)", ""),
                "Délai (j)": catalog_row.get("Délai (j)", ""),
                "Rédaction Ereferer": catalog_row.get("Rédaction Ereferer", ""),
                "Soumettre son article": catalog_row.get("Soumettre son article", ""),
                "Rédaction par le webmaster": catalog_row.get("Rédaction par le webmaster", ""),
            }
        )

    buyers_summary: list[dict] = []
    for target_domain, link_list in sorted(links_by_target.items()):
        sellers = {link["source_domain"] for link in link_list}
        source_urls = {link["source_url"] for link in link_list}
        catalog_row = catalog.get(target_domain, {})
        buyers_summary.append(
            {
                "domain": target_domain,
                "links_received_raw": len(link_list),
                "seller_domains_count": len(sellers),
                "articles_count": len(source_urls),
                "Visites": catalog_row.get("Visites", ""),
                "Trafic Google": catalog_row.get("Trafic Google", ""),
                "TF": catalog_row.get("TF", ""),
                "KW": catalog_row.get("KW", ""),
                "RD": catalog_row.get("RD", ""),
                "DA": catalog_row.get("DA", ""),
                "Catégorie": catalog_row.get("Catégorie", ""),
                "Langue": catalog_row.get("Langue", ""),
            }
        )

    network_edges = [
        {
            "source_domain": edge["source_domain"],
            "target_domain": edge["target_domain"],
            "links_count": edge["links_count"],
            "articles_count": len(edge["source_urls"]),
            "first_seen": edge["first_seen"],
            "last_seen": edge["last_seen"],
        }
        for edge in edge_stats.values()
    ]
    network_edges.sort(key=lambda item: item["links_count"], reverse=True)

    recent_links = sorted(
        link_events,
        key=lambda item: (item["detected_on"], item["source_domain"], item["target_domain"]),
        reverse=True,
    )[:1000]

    site_index = [
        {
            "domain": domain,
            "Visites": row.get("Visites", ""),
            "Trafic Google": row.get("Trafic Google", ""),
            "TF": row.get("TF", ""),
            "RD": row.get("RD", ""),
            "DA": row.get("DA", ""),
            "Catégorie": row.get("Catégorie", ""),
            "Langue": row.get("Langue", ""),
        }
        for domain, row in sorted(catalog.items())
    ]

    aggregates_dir = base_dir / "data" / "aggregates" / "latest"
    public_dir = base_dir / "public" / "data"
    payloads = {
        "sellers_summary.json": sellers_summary,
        "buyers_summary.json": buyers_summary,
        "links_recent.json": recent_links,
        "network_edges.json": network_edges,
        "site_index.json": site_index,
        "build_meta.json": {
            "generated_on": date.today().isoformat(),
            "seller_count": len(catalog),
            "buyer_count": len(buyers_summary),
            "page_events_count": len(page_events),
            "link_events_count": len(link_events),
        },
    }

    for filename, payload in payloads.items():
        write_json(aggregates_dir / filename, payload)
        write_json(public_dir / filename, payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(build())
