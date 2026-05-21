async function loadJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Failed to load ${path}`);
  }
  return response.json();
}

function renderCards(meta) {
  const cards = [
    ["Sites vendeurs suivis", meta.seller_count ?? 0],
    ["Domaines receveurs détectés", meta.buyer_count ?? 0],
    ["Pages analysées", meta.page_events_count ?? 0],
    ["Liens sortants détectés", meta.link_events_count ?? 0],
  ];
  const container = document.getElementById("summary-cards");
  container.innerHTML = cards
    .map(
      ([label, value]) => `
        <article class="card">
          <p>${label}</p>
          <strong>${value}</strong>
        </article>
      `,
    )
    .join("");

  document.getElementById("build-meta").innerHTML = `
    <span>Dernière génération</span>
    <strong>${meta.generated_on ?? "-"}</strong>
  `;
}

function renderGrid(target, columns, rows) {
  new gridjs.Grid({
    columns,
    data: rows,
    search: true,
    sort: true,
    pagination: { limit: 25 },
    resizable: true,
    autoWidth: true,
  }).render(document.getElementById(target));
}

async function main() {
  const [meta, sellers, buyers, links, edges] = await Promise.all([
    loadJson("./data/build_meta.json"),
    loadJson("./data/sellers_summary.json"),
    loadJson("./data/buyers_summary.json"),
    loadJson("./data/links_recent.json"),
    loadJson("./data/network_edges.json"),
  ]);

  renderCards(meta);

  renderGrid(
    "sellers-grid",
    [
      "Domaine",
      "Articles",
      "Articles avec liens",
      "Liens sortants",
      "Cibles uniques",
      "Visites",
      "Trafic Google",
      "TF",
      "RD",
      "DA",
      "Catégorie",
      "Langue",
    ],
    sellers.map((row) => [
      row.domain,
      row.articles_analyzed,
      row.articles_with_external_links,
      row.raw_outgoing_links_count,
      row.unique_target_domains_count,
      row["Visites"],
      row["Trafic Google"],
      row["TF"],
      row["RD"],
      row["DA"],
      row["Catégorie"],
      row["Langue"],
    ]),
  );

  renderGrid(
    "buyers-grid",
    [
      "Domaine",
      "Liens reçus",
      "Vendeurs distincts",
      "Articles distincts",
      "Visites",
      "Trafic Google",
      "TF",
      "RD",
      "DA",
      "Catégorie",
      "Langue",
    ],
    buyers.map((row) => [
      row.domain,
      row.links_received_raw,
      row.seller_domains_count,
      row.articles_count,
      row["Visites"],
      row["Trafic Google"],
      row["TF"],
      row["RD"],
      row["DA"],
      row["Catégorie"],
      row["Langue"],
    ]),
  );

  renderGrid(
    "links-grid",
    ["Date", "Source", "URL source", "Cible", "URL cible", "Anchor", "Rel", "Follow"],
    links.map((row) => [
      row.detected_on,
      row.source_domain,
      row.source_url,
      row.target_domain,
      row.target_url,
      row.anchor_text,
      (row.rel_flags || []).join(", "),
      row.is_follow ? "Oui" : "Non",
    ]),
  );

  renderGrid(
    "edges-grid",
    ["Source", "Cible", "Nb liens", "Nb articles", "Premier vu", "Dernier vu"],
    edges.map((row) => [
      row.source_domain,
      row.target_domain,
      row.links_count,
      row.articles_count,
      row.first_seen,
      row.last_seen,
    ]),
  );
}

main().catch((error) => {
  document.body.innerHTML = `<main class="page"><section class="panel"><h1>Erreur</h1><p>${error.message}</p></section></main>`;
});
