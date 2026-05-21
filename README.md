# Analyse VDL

Ce projet surveille des sitemaps de sites vendeurs de liens, détecte les nouvelles publications, extrait les liens sortants du contenu principal, puis génère des agrégats consultables dans un dashboard statique hébergeable sur Cloudflare Pages.

## Entrée

Le catalogue vendeur doit être stocké dans :

- `data/catalog/domains-vendeurs.csv`

Colonnes attendues, dans cet ordre exact :

```text
Site,Visites,Trafic Google,TF,KW,RD,DA,Catégorie,Langue,Taux publication (%),Délai (j),Rédaction Ereferer,Soumettre son article,Rédaction par le webmaster,Sitemap
```

## Pipeline

Chaque run :

1. lit le catalogue vendeur
2. lit les sitemaps de tous les sites
3. détecte les nouvelles URLs avec snapshots + ever_seen
4. initialise silencieusement les nouveaux sites sans notifier l'historique
5. télécharge seulement les nouvelles pages
6. extrait `title`, mot-clé, contenu principal et liens sortants
7. conserve uniquement les liens externes vers des domaines registrables différents
8. écrit des événements bruts dans `data/events/`
9. reconstruit les agrégats JSON pour le dashboard dans `public/data/`

## Structure

```text
data/
  catalog/
    domains-vendeurs.csv
  events/
    pages/
    links/
  aggregates/
    latest/
  state/
    snapshots/
    ever_seen/
public/
  index.html
  app.js
  styles.css
  data/
scripts/
  monitor_vdl.py
  build_dashboard_data.py
.github/
  workflows/
    vdl-monitor.yml
```

## Sorties

- `data/events/pages/YYYY-MM-DD.jsonl.gz`
- `data/events/links/YYYY-MM-DD.jsonl.gz`
- `public/data/sellers_summary.json`
- `public/data/buyers_summary.json`
- `public/data/links_recent.json`
- `public/data/network_edges.json`
- `public/data/site_index.json`

## Lancement local

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/monitor_vdl.py
python scripts/build_dashboard_data.py
```

## Déploiement

Le dossier `public/` peut être servi directement par Cloudflare Pages.
