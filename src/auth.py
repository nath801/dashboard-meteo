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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from src import config

logger = logging.getLogger(__name__)

ALGO = "pbkdf2_sha256"
ITERATIONS = 600_000          # recommandation OWASP 2023 pour PBKDF2-HMAC-SHA256
SALT_BYTES = 16
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
GENERIC_ERROR = "Identifiant ou mot de passe incorrect."
LOCKED_ERROR = "Trop de tentatives. Réessayez dans quelques minutes."


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
    return None


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
                 lockout_minutes: int = config.LOCKOUT_MINUTES) -> None:
        self.path = Path(path or config.USERS_FILE)
        self.max_failed = max_failed
        self.lockout_seconds = lockout_minutes * 60

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

    def add_user(self, username: str, password: str) -> None:
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

    def delete_user(self, username: str) -> bool:
        """Supprime un compte ; renvoie True s'il existait."""
        with self._lock:
            users = self._load()
            existed = users.pop(username.lower(), None) is not None
            if existed:
                self._save(users)
        return existed

    def authenticate(self, username: str, password: str) -> AuthResult:
        """Vérifie identifiant + mot de passe, avec verrouillage après trop d'échecs.

        Le message d'erreur est le même que le compte existe ou non (pas d'énumération).
        """
        key = (username or "").strip().lower()
        with self._lock:
            users = self._load()
            user = users.get(key)
            if user is None:
                verify_password(password, _DUMMY_HASH)  # même durée qu'un vrai compte
                return AuthResult(False, GENERIC_ERROR)

            now = time.time()
            if user.get("locked_until", 0) > now:
                return AuthResult(False, LOCKED_ERROR)

            if verify_password(password, user["hash"]):
                user["failed"], user["locked_until"] = 0, 0
                self._save(users)
                return AuthResult(True, "", key)

            user["failed"] = user.get("failed", 0) + 1
            if user["failed"] >= self.max_failed:
                user["failed"], user["locked_until"] = 0, now + self.lockout_seconds
                logger.warning("Compte verrouillé après trop d'échecs : %s", key)
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
