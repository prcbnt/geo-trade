# geo-trade

Carte interactive des balances commerciales bilatérales : biens (CEPII BACI, HS92, depuis 1995) et services (OCDE-OMC BaTIS, depuis 2005), avec l'UE-27 comme bloc et des avertissements sur les pays hubs.

- `scripts/build_data.py` télécharge les sources et produit `docs/data/` (un JSON par pays).
- `.github/workflows/build-data.yml` lance ce script (manuellement ou chaque 20 février) et commite les données.
- `docs/index.html` est la page servie par GitHub Pages (branche `main`, dossier `/docs`).

Sources : CEPII BACI (licence Etalab 2.0) ; OECD-WTO Balanced Trade in Services (BaTIS).
