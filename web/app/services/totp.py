"""TOTP (Time-based One-Time Password) utilities for two-factor authentication.

Uses pyotp (RFC 6238). A 30-second window with one step of tolerance is applied
so a code that was valid in the previous window still authenticates — this covers
the common case where the user reads the code just before it rolls over.
"""
from __future__ import annotations

import pyotp
import segno


def generate_secret() -> str:
    """Return a new random base32 TOTP secret (160 bits)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str, issuer: str = "Beacon") -> str:
    """Return an otpauth:// URI that authenticator apps can import."""
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def provisioning_qr_svg(uri: str) -> str:
    """Return the provisioning URI as an inline <svg> QR code.

    Black on white regardless of the UI theme: phone cameras want the maximum
    contrast and the canonical polarity, so the template frames it in a white
    tile rather than tinting the modules to match a dark page. The default
    four-module quiet zone is kept for the same reason. Error correction "M"
    is what authenticator apps expect for an otpauth URI of this length.
    """
    qr = segno.make(uri, error="m")
    return qr.svg_inline(scale=5, dark="#000000", light="#ffffff")


def verify(secret: str, code: str) -> bool:
    """True if code is valid for secret right now (one-step tolerance)."""
    if not secret or not code:
        return False
    totp = pyotp.TOTP(secret)
    # valid_window=1 accepts the previous and next 30-second windows as well,
    # so a code entered just as the window turns still works.
    return totp.verify(code.strip(), valid_window=1)
