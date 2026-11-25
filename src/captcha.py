"""Captcha image hébergé localement (Pillow) : aucun service tiers, compatible avec la CSP stricte.

Le texte attendu n'est jamais conservé en clair : seule son empreinte SHA-256 est gardée côté serveur
(dans la session Streamlit). Chaque captcha est à usage unique et expire après quelques minutes.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src import config

# Caractères sans ambiguïté visuelle (pas de 0/O, 1/I/L, 5/S…).
ALPHABET = "ABCDEFGHJKMNPQRTUVWXYZ2346789"
LENGTH = 5
WIDTH, HEIGHT = 220, 80
_rng = secrets.SystemRandom()


def _digest(text: str) -> str:
    """Empreinte du texte normalisé (majuscules, sans espaces)."""
    normalized = "".join((text or "").split()).upper()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render(text: str) -> bytes:
    """Dessine le texte déformé (rotation, décalage, lignes et points parasites) et renvoie un PNG."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (245, 247, 250))
    draw = ImageDraw.Draw(img)
    for _ in range(6):  # lignes parasites derrière le texte
        draw.line([(_rng.randint(0, WIDTH), _rng.randint(0, HEIGHT)),
                   (_rng.randint(0, WIDTH), _rng.randint(0, HEIGHT))],
                  fill=tuple(_rng.randint(120, 200) for _ in range(3)), width=2)
    font = _font(42)
    step = (WIDTH - 20) // len(text)
    for i, char in enumerate(text):
        glyph = Image.new("RGBA", (64, 84), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).text((10, 14), char, font=font,
                                   fill=tuple(_rng.randint(10, 90) for _ in range(3)) + (255,))
        glyph = glyph.rotate(_rng.uniform(-30, 30), resample=Image.BICUBIC, expand=False)
        img.paste(glyph, (10 + i * step + _rng.randint(-4, 4), _rng.randint(-12, -2)), glyph)
    for _ in range(3):  # lignes parasites devant le texte
        draw.arc([_rng.randint(-40, 40), _rng.randint(-20, 40), _rng.randint(150, 260), _rng.randint(60, 120)],
                 _rng.randint(0, 180), _rng.randint(180, 360),
                 fill=tuple(_rng.randint(40, 120) for _ in range(3)), width=2)
    for _ in range(250):  # bruit
        draw.point((_rng.randrange(WIDTH), _rng.randrange(HEIGHT)),
                   fill=tuple(_rng.randint(0, 255) for _ in range(3)))
    img = img.filter(ImageFilter.SMOOTH)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@dataclass
class Captcha:
    """Un défi captcha : image à afficher + empreinte de la réponse + date d'expiration."""

    image: bytes
    answer_digest: str
    expires_at: float
    used: bool = field(default=False)

    @classmethod
    def new(cls, ttl_seconds: int = config.CAPTCHA_TTL_SECONDS, text: Optional[str] = None) -> "Captcha":
        """Crée un captcha aléatoire (``text`` ne sert qu'aux tests)."""
        text = text or "".join(_rng.choice(ALPHABET) for _ in range(LENGTH))
        return cls(render(text), _digest(text), time.time() + ttl_seconds)

    def verify(self, answer: str, now: Optional[float] = None) -> bool:
        """Vérifie la réponse (insensible à la casse et aux espaces). Usage unique, expiration."""
        if self.used:
            return False
        self.used = True
        if (now or time.time()) > self.expires_at or not answer:
            return False
        return hmac.compare_digest(_digest(answer), self.answer_digest)
