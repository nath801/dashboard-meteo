# Rapports de tests

Exécution du 25/11/2026 (Windows 11, Python 3.12.10, pytest 9.2.0) : **41 tests, 41 réussis, 0 échec**.

| Fichier | Contenu |
|---|---|
| `pytest_resultats.txt` | Sortie complète de `pytest -v` : résultat de chaque test + tableau de couverture du code |
| `junit.xml` | Même résultat au format JUnit XML (format standard lu par GitLab, Jenkins, GitHub…) |

## Répartition
| Fichier de tests | Tests | Ce qui est vérifié |
|---|---|---|
| `tests/test_api.py` | 9 | Cache SQLite (écriture, TTL, entrée corrompue), API → cache, coupure réseau → données simulées, HTTP 500, JSON corrompu, réponse `error` de l'API, format des données simulées |
| `tests/test_processing.py` | 19 | DataFrame, interpolation et valeurs négatives, score de confort, statistiques, tendances, alertes OMS (NO2, PM2.5, canicule), 7 catégories AQI |
| `tests/test_auth.py` | 13 | Hachage PBKDF2 salé, politique de mot de passe, anti-énumération, verrouillage, limite par IP, journal de sécurité sans mot de passe ni injection |

Le réseau est simulé (`unittest.mock`) : les tests ne dépendent pas d'Internet et n'écrivent jamais dans `data/`.

## Couverture du code (pytest-cov)
| Module | Couverture |
|---|---|
| `config.py` | 92 % |
| `alerts.py` | 91 % |
| `auth.py` | 81 % (la partie non couverte est surtout la ligne de commande `python -m src.auth`) |
| `api_client.py` | 79 % (géocodage non testé unitairement) |
| `data_processing.py` | 77 % (tableau des tendances et synthèse journalière) |
| `dashboard.py` | 0 % — interface Streamlit, vérifiée dans le navigateur (Playwright : tous les onglets, aucune exception, 0 violation CSP) |
| **Total** | **57 %** (≈ 82 % hors interface) |

## Reproduire
```powershell
python -m pip install pytest-cov
python -m pytest -v --junitxml=docs/tests/junit.xml --cov=src --cov-report=term-missing
```
Dans la CI (`.github/workflows/ci-securite.yml`, job « Tests unitaires »), ces rapports sont produits à chaque
exécution et publiés comme artefact `rapport-tests`.
