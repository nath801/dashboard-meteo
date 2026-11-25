"""Tests du captcha : image, réponse, usage unique, expiration, non-stockage en clair."""
import io
import time

from PIL import Image

from src.auth import IPRateLimiter
from src.captcha import ALPHABET, LENGTH, Captcha


def test_image_is_a_png_with_random_text():
    c1, c2 = Captcha.new(), Captcha.new()
    img = Image.open(io.BytesIO(c1.image))
    assert img.format == "PNG" and img.size == (220, 80)
    assert c1.answer_digest != c2.answer_digest  # 29^5 ≈ 20 millions de combinaisons
    assert set(ALPHABET).isdisjoint("0O1IL5S") and LENGTH == 5


def test_correct_answer_case_and_spaces_insensitive():
    assert Captcha.new(text="AB3CD").verify(" ab3 cd ")


def test_answer_is_not_stored_in_clear():
    c = Captcha.new(text="XK7PQ")
    assert "XK7PQ" not in repr(c.__dict__)


def test_wrong_or_empty_answer_is_rejected():
    assert not Captcha.new(text="AB3CD").verify("AB3CE")
    assert not Captcha.new(text="AB3CD").verify("")
    assert not Captcha.new(text="AB3CD").verify(None)


def test_captcha_is_single_use():
    c = Captcha.new(text="AB3CD")
    assert not c.verify("ZZZZZ")
    assert not c.verify("AB3CD")  # déjà utilisé : on ne peut pas essayer plusieurs réponses


def test_captcha_expires():
    c = Captcha.new(ttl_seconds=60, text="AB3CD")
    assert not c.verify("AB3CD", now=time.time() + 61)


def test_ip_counter_for_captcha_and_signup_limit():
    limiter = IPRateLimiter(max_failures=3, window_minutes=60)
    for _ in range(2):
        limiter.record_failure("10.0.0.1")
    assert limiter.count("10.0.0.1") == 2 and not limiter.is_blocked("10.0.0.1")
    limiter.record_failure("10.0.0.1")
    assert limiter.is_blocked("10.0.0.1") and limiter.count("10.0.0.2") == 0
