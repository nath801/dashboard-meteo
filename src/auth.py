"""Authentification : comptes, mots de passe hachés (PBKDF2), verrouillage anti-bruteforce.

Création d'un compte en ligne de commande (mot de passe saisi de façon masquée) :
    python -m src.auth add <identifiant>
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from src import config

logger = logging.getLogger(__name__)

ALGO = "pbkdf2_sha256"
ITERATIONS = 600_000          # recommandation OWASP 2023 pour PBKDF2-HMAC-SHA256
SALT_BYTES = 16
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
GENERIC_ERROR = "Identifiant ou mot de passe incorrect."
LOCKED_ERROR = "Trop de tentatives. Réessayez dans quelques minutes."

# Mots de passe très courants refusés même s'ils respectent la politique (V12).
COMMON_PASSWORDS = {
    "password1", "password123", "passw0rd", "motdepasse1", "motdepasse123", "azerty123", "azerty1234",
    "azertyuiop1", "qwerty123", "qwerty1234", "abc12345", "abcd1234", "admin123", "admin1234",
    "administrateur1", "bonjour123", "soleil123", "welcome1", "welcome123", "letmein1", "iloveyou1",
    "123456789a", "a123456789", "1q2w3e4r", "1qaz2wsx", "zaq12wsx", "changeme1", "test1234",
    "dashboard1", "meteo2026", "paris2024", "football1", "doudou123", "loulou123", "nicolas1",
}


# --------------------------------------------------------------------- Hachage
def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    """Hache un mot de passe : `pbkdf2_sha256$iter$sel_hex$hash_hex` (sel aléatoire de 16 octets)."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Vérifie un mot de passe en temps constant ; False si le hachage stocké est mal formé."""
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != ALGO:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# Hachage factice : calculé pour un identifiant inconnu afin que le temps de réponse soit identique.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


# --------------------------------------------------------------------- Règles
def validate_username(username: str) -> Optional[str]:
    """Renvoie un message d'erreur si l'identifiant est invalide, sinon None."""
    if not USERNAME_RE.fullmatch(username or ""):
        return "Identifiant : 3 à 32 caractères parmi lettres, chiffres, « . », « _ » et « - »."
    return None


def validate_password(password: str) -> Optional[str]:
    """Politique de mot de passe : 8 caractères minimum, au moins une lettre et un chiffre."""
    if len(password) < 8:
        return "Le mot de passe doit contenir au moins 8 caractères."
    if len(password) > 128:
        return "Le mot de passe ne doit pas dépasser 128 caractères."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Le mot de passe doit contenir au moins une lettre et un chiffre."
    if password.lower() in COMMON_PASSWORDS:
        return "Ce mot de passe est trop courant : choisissez-en un autre."
    return None


# --------------------------------------------------------------------- Journal de sécurité (V8)
_sec_logger = logging.getLogger("security")


def _sanitize(value: Any) -> str:
    """Neutralise les retours à la ligne / tabulations (anti-injection dans le journal)."""
    return re.sub(r"[\r\n\t]", "_", str(value))[:100]


def security_event(event: str, username: str = "-", ip: str = "-", **extra: Any) -> None:
    """Écrit un événement de sécurité (jamais de mot de passe) dans data/security.log."""
    if not _sec_logger.handlers:
        config.SECURITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(config.SECURITY_LOG, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _sec_logger.addHandler(handler)
        _sec_logger.setLevel(logging.INFO)
        _sec_logger.propagate = False
    details = " ".join(f"{k}={_sanitize(v)}" for k, v in extra.items())
    _sec_logger.info("%s user=%s ip=%s %s", event, _sanitize(username), _sanitize(ip), details)


# --------------------------------------------------------------------- Limite par IP (V7)
class IPRateLimiter:
    """Bloque une adresse IP après trop d'échecs sur une fenêtre glissante (password spraying)."""

    def __init__(self, max_failures: int = config.IP_MAX_FAILURES,
                 window_minutes: int = config.IP_WINDOW_MINUTES) -> None:
        self.max_failures = max_failures
        self.window = window_minutes * 60
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _purge(self, ip: str, now: float) -> Deque[float]:
        q = self._failures[ip]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def is_blocked(self, ip: str) -> bool:
        """True si l'IP a atteint le nombre maximal d'échecs dans la fenêtre."""
        with self._lock:
            return len(self._purge(ip, time.time())) >= self.max_failures

    def count(self, ip: str) -> int:
        """Nombre d'événements enregistrés pour cette IP dans la fenêtre."""
        with self._lock:
            return len(self._purge(ip, time.time()))

    def record_failure(self, ip: str) -> None:
        """Enregistre un échec de connexion pour cette IP."""
        with self._lock:
            now = time.time()
            self._purge(ip, now).append(now)

    def reset(self, ip: str) -> None:
        """Efface les échecs d'une IP (après une connexion réussie)."""
        with self._lock:
            self._failures.pop(ip, None)


# --------------------------------------------------------------------- Stockage
@dataclass
class AuthResult:
    """Résultat d'une tentative de connexion."""

    ok: bool
    message: str = ""
    username: Optional[str] = None


class UserStore:
    """Comptes utilisateurs dans un fichier JSON (écriture atomique, verrou entre threads)."""

    _lock = threading.Lock()

    def __init__(self, path: Path | str | None = None,
                 max_failed: int = config.MAX_FAILED_ATTEMPTS,
                 lockout_minutes: int = config.LOCKOUT_MINUTES,
                 ip_limiter: Optional[IPRateLimiter] = None) -> None:
        self.path = Path(path or config.USERS_FILE)
        self.max_failed = max_failed
        self.lockout_seconds = lockout_minutes * 60
        self.ip_limiter = ip_limiter or IPRateLimiter()

    # -- fichier
    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.error("Fichier des comptes illisible : %s", self.path)
            return {}

    def _save(self, users: Dict[str, Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(users, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # remplacement atomique

    # -- comptes
    def has_users(self) -> bool:
        """Indique si au moins un compte existe."""
        return bool(self._load())

    def add_user(self, username: str, password: str, ip: str = "cli") -> None:
        """Crée un compte ; lève ValueError si l'identifiant ou le mot de passe est refusé."""
        err = validate_username(username) or validate_password(password)
        if err:
            raise ValueError(err)
        key = username.lower()
        with self._lock:
            users = self._load()
            if key in users:
                raise ValueError("Cet identifiant est déjà utilisé.")
            users[key] = {"hash": hash_password(password), "failed": 0, "locked_until": 0,
                          "created": dt.datetime.now().isoformat(timespec="seconds")}
            self._save(users)
        logger.info("Compte créé : %s", key)
        security_event("ACCOUNT_CREATED", key, ip)

    def delete_user(self, username: str) -> bool:
        """Supprime un compte ; renvoie True s'il existait."""
        with self._lock:
            users = self._load()
            existed = users.pop(username.lower(), None) is not None
            if existed:
                self._save(users)
        return existed

    def authenticate(self, username: str, password: str, ip: str = "-") -> AuthResult:
        """Vérifie identifiant + mot de passe, avec verrouillage du compte et limite par IP.

        Le message d'erreur est le même que le compte existe ou non (pas d'énumération).
        """
        key = (username or "").strip().lower()[:64]
        if self.ip_limiter.is_blocked(ip):
            security_event("LOGIN_BLOCKED_IP", key, ip)
            return AuthResult(False, LOCKED_ERROR)
        with self._lock:
            users = self._load()
            user = users.get(key)
            if user is None:
                verify_password(password, _DUMMY_HASH)  # même durée qu'un vrai compte
                self.ip_limiter.record_failure(ip)
                security_event("LOGIN_FAILED", key, ip, reason="unknown_user")
                return AuthResult(False, GENERIC_ERROR)

            now = time.time()
            if user.get("locked_until", 0) > now:
                security_event("LOGIN_FAILED", key, ip, reason="account_locked")
                return AuthResult(False, LOCKED_ERROR)

            if verify_password(password, user["hash"]):
                user["failed"], user["locked_until"] = 0, 0
                self._save(users)
                self.ip_limiter.reset(ip)
                security_event("LOGIN_SUCCESS", key, ip)
                return AuthResult(True, "", key)

            self.ip_limiter.record_failure(ip)
            user["failed"] = user.get("failed", 0) + 1
            security_event("LOGIN_FAILED", key, ip, reason="bad_password", count=user["failed"])
            if user["failed"] >= self.max_failed:
                user["failed"], user["locked_until"] = 0, now + self.lockout_seconds
                security_event("ACCOUNT_LOCKED", key, ip, minutes=self.lockout_seconds // 60)
            self._save(users)
            return AuthResult(False, GENERIC_ERROR)


# --------------------------------------------------------------------- CLI
def main(argv: Optional[list] = None) -> int:
    """Gestion des comptes en ligne de commande : add / delete / list."""
    parser = argparse.ArgumentParser(prog="python -m src.auth", description="Gestion des comptes du dashboard")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add", help="créer un compte").add_argument("username")
    sub.add_parser("delete", help="supprimer un compte").add_argument("username")
    sub.add_parser("list", help="lister les comptes")
    args = parser.parse_args(argv)
    store = UserStore()

    if args.cmd == "add":
        pwd = getpass.getpass("Mot de passe : ")
        if pwd != getpass.getpass("Confirmer le mot de passe : "):
            print("Les mots de passe ne correspondent pas.", file=sys.stderr)
            return 1
        try:
            store.add_user(args.username, pwd)
        except ValueError as exc:
            print(f"Refusé : {exc}", file=sys.stderr)
            return 1
        print(f"Compte « {args.username.lower()} » créé.")
    elif args.cmd == "delete":
        print("Compte supprimé." if store.delete_user(args.username) else "Compte introuvable.")
    else:
        for name, info in store._load().items():
            print(f"{name}  (créé le {info.get('created', '?')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
