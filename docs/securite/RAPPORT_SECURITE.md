# Rapport de sécurité — Dashboard Météo & Qualité de l'Air

Audit réalisé le 25/11/2026 sur l'application en local : version du matin (`http://localhost:8501`),
puis version corrigée (`https://localhost:8443`, Streamlit derrière le reverse proxy Caddy).

> **Message clé** : Bandit et pip-audit n'ont **rien** trouvé, et ZAP n'a relevé que des en-têtes HTTP
> manquants. Les deux failles les plus graves (V1 inscription libre, V2 absence de HTTPS) n'ont été
> trouvées **que par la revue manuelle**.

## 1. Méthode et outils

| Type | Outil | Ce qu'il vérifie |
|---|---|---|
| DAST (application en marche) | OWASP ZAP 2.17 (scan rapide : spider + scan actif) | En-têtes HTTP, injections, XSS, fuites d'informations |
| SAST (code source) | Bandit 1.9.4 | eval/exec, injection SQL, subprocess, secrets en dur, hachages faibles |
| SCA (dépendances) | pip-audit 2.10.1 | CVE connues dans `requirements.txt` |
| Secrets | Gitleaks (dans la CI) | Clés ou mots de passe dans l'historique Git |
| Revue manuelle | curl, netstat, lecture du code | Authentification, configuration serveur, Docker |

**SonarQube n'a pas été retenu** : il nécessite un serveur (Java + base de données ou Docker). La consigne
autorise « SonarQube ou autres » : Bandit couvre l'analyse statique de sécurité du Python sans
infrastructure et s'intègre directement dans la CI.

## 2. Résultats avant correctifs

| Outil | Résultat |
|---|---|
| Bandit (`bandit_avant.txt`) | 0 problème (930 lignes analysées) |
| pip-audit | Aucune CVE connue |
| OWASP ZAP (`zap_avant.html`) | 0 haute · **1 moyenne** (CSP absente) · 2 faibles (X-Content-Type-Options absent, Timestamp Disclosure) · 2 informatives |
| `curl -D -` | En-tête `server: uvicorn`, aucun `X-Frame-Options`, aucune CSP, pas de HTTPS |
| `netstat` | Streamlit écoute sur `0.0.0.0:8501` et `[::]:8501` (toutes les interfaces) |
| WebSocket avec `Host: evil.example` | **101** (accepté) |
| Revue du code | Inscription ouverte à tous, pas de limite par IP, pas de journal, mots de passe courants acceptés, dépendances en `>=` |

Remarque : lors d'un premier passage du TP, ZAP signalait aussi l'absence de protection anti-clickjacking.
Cette fois, ZAP 2.17 ne l'a pas remontée ; elle a été trouvée par l'inspection des en-têtes (curl).

## 3. Vulnérabilités identifiées

| # | Vulnérabilité | Trouvée par | Gravité | OWASP Top 10 2021 | Correctif |
|---|---|---|---|---|---|
| V1 | Inscription libre | Revue | Haute | A07 Identification & Auth. | `ALLOW_SIGNUP=false`, comptes via CLI |
| V2 | Pas de HTTPS | Revue | Haute | A02 Défaillances crypto. | Caddy HTTPS + HSTS |
| V3 | CSP absente | ZAP | Moyenne | A05 Mauvaise configuration | CSP stricte via Caddy |
| V4 | Anti-clickjacking absent | Inspection HTTP | Moyenne | A05 | `X-Frame-Options: DENY`, `frame-ancestors 'none'` |
| V5 | Streamlit sur toutes les interfaces, Host non filtré | Revue / netstat | Moyenne | A05 | `address=127.0.0.1`, `allowedHosts`, XSRF strict |
| V6 | Docker en root, sans `.dockerignore` | Revue | Moyenne | A05 | `.dockerignore`, `USER appuser`, `read_only`, `cap_drop: ALL` |
| V7 | Pas de limite par IP (password spraying) | Revue | Moyenne | A07 | `IPRateLimiter` (20 échecs / 15 min) |
| V8 | Pas de journal de sécurité | Revue | Moyenne | A09 Journalisation | `data/security.log` |
| V9 | Traces d'erreur affichées | Revue | Faible | A05 | `client.showErrorDetails = "none"` |
| V10 | X-Content-Type-Options absent | ZAP | Faible | A05 | `nosniff` |
| V11 | En-tête `Server: uvicorn` | Inspection HTTP | Faible | A05 | `-Server`, `-Via` dans Caddy |
| V12 | Mots de passe courants acceptés | Revue | Faible | A07 | Liste de mots de passe interdits |
| V13 | Dépendances non figées (`>=`) | Revue | Faible | A06 Composants vulnérables | Versions `==` + pip-audit en CI |

### V1 — Inscription libre (Haute)
**Risque** : l'onglet « Créer un compte » permettait à n'importe quel visiteur de se créer un compte : la page
de connexion ne protégeait donc rien. **Correctif** : inscription désactivée par défaut (`ALLOW_SIGNUP=false`
dans `config.py`) ; l'administrateur crée les comptes avec `python -m src.auth add <identifiant>` (mot de passe
saisi avec `getpass`). **Vérification** : test Playwright — l'onglet d'inscription n'est plus affiché.

**Évolution — inscription en option avec captcha.** À la demande, la création de compte en ligne a été rendue
disponible comme **option** (`ALLOW_SIGNUP=true` dans `.env`, toujours `false` par défaut). Elle est alors protégée par :
- un **captcha image** (`src/captcha.py`, Pillow) généré sur le serveur : aucun service tiers (pas de reCAPTCHA :
  pas de traçage des visiteurs ni d'exception à la CSP), seule l'empreinte SHA-256 de la réponse est gardée en session,
  usage unique (une seule réponse possible par image) et expiration après 5 minutes ;
- une **limite de 3 comptes par adresse IP et par heure** (événement `SIGNUP_BLOCKED_IP`) ;
- la politique de mot de passe existante (V12) et le journal (`ACCOUNT_CREATED`, `CAPTCHA_FAILED`).
Le captcha est aussi demandé **à la connexion après 3 échecs** (dans la session ou depuis la même IP), en complément
du verrouillage du compte et de la limite par IP (V7).
**Vérification** : 7 tests unitaires (`tests/test_captcha.py`) ; test Playwright en HTTPS via Caddy — mauvais captcha
refusé, compte créé avec le bon captcha, captcha affiché après 3 échecs de connexion puis connexion réussie,
0 violation CSP (compte de test supprimé ensuite).
**Risque résiduel** : un captcha « maison » peut être lu par un logiciel d'OCR ; il freine les robots simples mais
ne remplace pas la décision de fond : sur un service réel, l'inscription libre reste désactivée ou soumise à validation.

### V2 — Pas de HTTPS (Haute)
**Risque** : identifiant et mot de passe circulent en clair (HTTP) et le cookie de session peut être intercepté.
**Correctif** : reverse proxy Caddy sur `https://localhost:8443` (`tls internal`) + HSTS 1 an.
**Vérification** : `curl -sk -D - https://localhost:8443/` → 200 avec `Strict-Transport-Security`.

### V3 — Content-Security-Policy absente (Moyenne)
**Risque** : en cas d'injection HTML, rien n'empêche l'exécution de scripts tiers (XSS). **Correctif** : CSP stricte :
scripts du site uniquement + le seul script inline de Streamlit autorisé par son empreinte
`sha256-THxz9J9+…`, tuiles `https://tile.openstreetmap.org`, `frame-ancestors 'none'`, `object-src 'none'`.
`'unsafe-eval'` s'est révélé inutile. **Vérification** : test Playwright de tous les onglets en HTTPS → **0 violation CSP**.

### V4 — Pas de protection anti-clickjacking (Moyenne)
**Risque** : le dashboard peut être intégré dans une iframe invisible d'un site malveillant. **Correctif** :
`X-Frame-Options: DENY` et `frame-ancestors 'none'`. **Vérification** : en-têtes présents (curl) ; règle ZAP 10020 en FAIL dans la CI.

### V5 — Exposition réseau et Host non filtré (Moyenne)
**Risque** : Streamlit écoutait sur toutes les interfaces (accessible depuis le réseau local sans HTTPS) et
acceptait n'importe quel en-tête Host (DNS rebinding). **Correctif** (`.streamlit/config.toml`) :
`address = "127.0.0.1"`, `allowedHosts = ["localhost","127.0.0.1"]`, `xsrfCookieSameSite = "strict"`.
**Vérification** : `netstat` → `127.0.0.1:8501` seulement ; WebSocket `Host: evil.example` → **403**, `Host: localhost:8501` → **101**.

### V6 — Conteneur Docker en root (Moyenne)
**Risque** : sans `.dockerignore`, `.env`, `data/users.json` et `.git` seraient copiés dans l'image ; un processus
root compromis a plus de pouvoir. **Correctif** : `.dockerignore`, `useradd --uid 10001 appuser`, `USER appuser`,
`read_only`, `tmpfs`, `no-new-privileges`, `cap_drop: [ALL]`, Streamlit non publié (seul Caddy expose 8443).
**Vérification** : relecture. **Le build n'a pas pu être testé** : Docker Desktop refuse de démarrer car le compte Windows n'est pas membre du groupe `docker-users` (ajout réservé à un administrateur).

### V7 — Pas de limite par IP (Moyenne)
**Risque** : le verrouillage par compte n'empêche pas d'essayer un mot de passe courant sur de nombreux comptes
(password spraying). **Correctif** : `IPRateLimiter` — 20 échecs par IP sur 15 min, IP lue dans `X-Forwarded-For`
(posé par Caddy). **Vérification** : test `test_ip_rate_limit_blocks_spraying`.

### V8 — Pas de journal de sécurité (Moyenne)
**Risque** : aucune trace d'une attaque. **Correctif** : `data/security.log` avec ACCOUNT_CREATED, LOGIN_SUCCESS,
LOGIN_FAILED, ACCOUNT_LOCKED, LOGIN_BLOCKED_IP, LOGOUT, SESSION_EXPIRED et l'IP ; jamais de mot de passe ;
`\r\n\t` remplacés (anti-injection de lignes). **Vérification** : test `test_security_log_has_events_but_no_password`
et journal réel : `LOGIN_FAILED user=attaquant ip=::1 reason=unknown_user`.

### V9 — Traces d'erreur affichées (Faible)
**Risque** : une exception affiche la pile Python (chemins, code). **Correctif** : `showErrorDetails = "none"`.

### V10 — X-Content-Type-Options absent (Faible)
**Risque** : le navigateur peut « deviner » le type d'un fichier (MIME sniffing). **Correctif** : `nosniff`.
**Vérification** : l'alerte disparaît du rapport ZAP après correctifs.

### V11 — Divulgation du serveur (Faible)
**Risque** : `Server: uvicorn` aide un attaquant à cibler des failles connues. **Correctif** : `-Server`, `-Via`,
`-X-Powered-By` dans Caddy. **Vérification** : plus d'en-tête `Server` dans la réponse.

### V12 — Mots de passe courants acceptés (Faible)
**Risque** : « Password123 » respectait la politique (≥ 8 caractères, lettre + chiffre). **Correctif** : liste de
mots de passe interdits. **Vérification** : cas paramétré dans `test_password_policy`.

### V13 — Dépendances non figées (Faible)
**Risque** : `>=` installe des versions non testées, potentiellement compromises. **Correctif** : versions `==`,
contrôlées chaque semaine par pip-audit dans la CI.

## 4. Résultats après correctifs

| Outil | Avant | Après |
|---|---|---|
| OWASP ZAP | 0 haute · 1 moyenne · 2 faibles · 2 info | 0 haute · 1 moyenne · 1 faible · 2 info |
| Bandit | 0 problème | 0 problème (`bandit_apres.txt`) |
| pip-audit | aucune CVE | aucune CVE (`pip_audit.txt`) |
| pytest | 38 tests | **41 tests réussis** |
| Violations CSP (navigateur) | — | 0 |

Le nombre d'alertes moyennes reste à 1, mais ce n'est plus la même : « CSP absente » est remplacée par
« CSP : style-src unsafe-inline ».

### Risques résiduels acceptés
- **CSP `style-src 'unsafe-inline'`** (ZAP, moyenne) : Streamlit génère son CSS dynamiquement, la page est
  inutilisable sans. Un style ne peut pas exécuter de JavaScript ; les scripts restent strictement limités.
- **Timestamp Disclosure** (ZAP, faible) : faux positif — nombres présents dans le JavaScript compilé de Streamlit.
- **Certificat local** : avertissement du navigateur en développement ; en production Caddy obtient un certificat Let's Encrypt.
- **Comptes en fichier JSON, limiteur IP en mémoire** : suffisant pour quelques utilisateurs ; en production,
  base de données ou fournisseur d'identité (OIDC).
- **Scan ZAP non authentifié** : ZAP n'analyse que la page de connexion et les ressources publiques.

## 5. CI/CD
`.github/workflows/ci-securite.yml` (push sur main, pull request, manuel, chaque lundi) :

| Job | Outil | Échoue si… |
|---|---|---|
| tests | pytest | un test échoue |
| sast | Bandit `-ll` (+ rapport HTML en artefact) | gravité moyenne ou haute |
| dependances | pip-audit | une CVE est connue |
| secrets | Gitleaks | un secret est dans l'historique |
| dast | ZAP baseline sur `https://localhost:8443` (Streamlit + Caddy lancés dans le job) | une règle FAIL de `.zap/rules.tsv` réapparaît |

`.zap/rules.tsv` met en **FAIL** les règles corrigées (10020 X-Frame-Options, 10021 nosniff, 10035 HSTS,
10036 Server, 10037 X-Powered-By, 10038 CSP, 10063 Permissions-Policy) : les correctifs deviennent des tests de
non-régression. 10055 (unsafe-inline) est en WARN, les faux positifs/informatifs (10096, 10027, 10109, 10049) en IGNORE.

**Exécution sur GitHub** (dépôt privé) : le premier pipeline a échoué sur le job Gitleaks sans secret trouvé —
l'action `gitleaks-action` analyse `premier_commit^..dernier` et le commit initial n'a pas de parent. Gitleaks est
maintenant lancé en ligne de commande sur tout l'historique (14 commits, aucune fuite). Le pipeline suivant est
**vert sur les 5 jobs** (capture `docs/captures/10_ci_github.png`).

## 6. Commandes pour reproduire
```powershell
python -m bandit -r src app.py
python -m pip_audit -r requirements.txt
python -m pytest -v
streamlit run app.py
caddy run --config Caddyfile --adapter caddyfile
curl.exe -sk -D - -o NUL https://localhost:8443/
netstat -ano | findstr 8501
# ZAP (Java 21) :
java -Djdk.net.unixdomain.tmpdir=C:\Users\Public\zaptmp -Djava.io.tmpdir=C:\Users\Public\zaptmp `
     -jar zap-2.17.0.jar -cmd -quickurl https://localhost:8443 -quickout zap_apres.html
```
