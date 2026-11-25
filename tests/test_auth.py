"""Tests de l'authentification : hachage, politique, stockage, anti-énumération, verrouillage."""
import pytest

from src import auth, config
from src.auth import (GENERIC_ERROR, IPRateLimiter, UserStore, hash_password, validate_password,
                      verify_password)

# Mot de passe de test valide (absent des listes de mots de passe courants).
PWD = "Gr4nit-Lune-58"


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users.json", max_failed=3, lockout_minutes=5,
                     ip_limiter=IPRateLimiter(max_failures=4, window_minutes=15))


def test_hash_is_salted_and_verifiable():
    h1, h2 = hash_password(PWD, iterations=1000), hash_password(PWD, iterations=1000)
    assert h1 != h2                      # sel aléatoire
    assert h1.startswith("pbkdf2_sha256$1000$")
    assert verify_password(PWD, h1)
    assert not verify_password("autre-Mot2passe", h1)


def test_malformed_hash_is_rejected():
    for bad in ["", "pas-un-hash", "md5$1$00$00", "pbkdf2_sha256$x$zz$00", None]:
        assert verify_password(PWD, bad) is False


@pytest.mark.parametrize("password, ok", [
    ("court1", False),            # trop court
    ("sanschiffres", False),      # pas de chiffre
    ("1234567890", False),        # pas de lettre
    ("Password123", False),       # mot de passe courant (liste interdite)
    (PWD, True),
])
def test_password_policy(password, ok):
    assert (validate_password(password) is None) is ok


def test_password_never_stored_in_clear(store):
    store.add_user("Alice", PWD)
    content = store.path.read_text(encoding="utf-8")
    assert PWD not in content
    assert "pbkdf2_sha256$600000$" in content
    assert '"alice"' in content          # identifiant normalisé en minuscules


def test_username_validation_and_duplicates(store):
    with pytest.raises(ValueError):
        store.add_user("a b<script>", PWD)
    store.add_user("bob", PWD)
    with pytest.raises(ValueError, match="déjà utilisé"):
        store.add_user("BOB", PWD)       # insensible à la casse


def test_generic_message_for_unknown_user_and_bad_password(store):
    store.add_user("carol", PWD)
    unknown = store.authenticate("inconnu", PWD)
    wrong = store.authenticate("carol", "Mauvais-mdp-1")
    assert not unknown.ok and not wrong.ok
    assert unknown.message == wrong.message == GENERIC_ERROR
    assert store.authenticate("CAROL", PWD).ok


def test_account_lockout_then_unlock(store, monkeypatch):
    store.add_user("dave", PWD)
    clock = [1_000_000.0]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    for _ in range(3):
        assert not store.authenticate("dave", "Mauvais-mdp-1").ok
    locked = store.authenticate("dave", PWD)          # bon mot de passe mais compte verrouillé
    assert not locked.ok and locked.message == auth.LOCKED_ERROR
    clock[0] += 5 * 60 + 1                              # 5 minutes plus tard
    assert store.authenticate("dave", PWD).ok


def test_ip_rate_limit_blocks_spraying(store):
    """Password spraying : une IP qui essaie plusieurs comptes est bloquée, les autres IP non."""
    store.add_user("erin", PWD)
    for name in ["u1", "u2", "u3", "u4"]:
        store.authenticate(name, "Essai-mdp-1", ip="203.0.113.9")
    blocked = store.authenticate("erin", PWD, ip="203.0.113.9")
    assert not blocked.ok and blocked.message == auth.LOCKED_ERROR
    assert store.authenticate("erin", PWD, ip="198.51.100.7").ok


def test_security_log_has_events_but_no_password(store):
    store.add_user("frank", PWD, ip="127.0.0.1")
    store.authenticate("frank", "Mauvais-mdp-1", ip="127.0.0.1")
    store.authenticate("frank\nFAKE LOGIN_SUCCESS user=admin", "x", ip="127.0.0.1")
    store.authenticate("frank", PWD, ip="127.0.0.1")
    log = config.SECURITY_LOG.read_text(encoding="utf-8")
    assert "ACCOUNT_CREATED user=frank" in log
    assert "LOGIN_FAILED user=frank ip=127.0.0.1" in log
    assert "LOGIN_SUCCESS user=frank" in log
    assert PWD not in log and "Mauvais-mdp-1" not in log
    assert not any(line.startswith("FAKE") for line in log.splitlines())  # pas d'injection de ligne
