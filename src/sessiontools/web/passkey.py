"""The front door's passkey (WebAuthn) checks, and nothing else.

PROPOSED (door-passkey). This is the ONE module of sessiontools that
imports `cryptography`, which comes with the `passkey` extra
(`sessiontools[passkey]`; in the kit it is already there through
sessionphone). Nothing else imports this module except the front door,
and the door imports it only when a passkey is asked for, so
`import sessiontools` and every other `web/` module stay stdlib-only.

The checks are sessionphone's `web/passkeys.py` (itself copied from the
settings app's lock, `webauth.py`), and only its pure functions. The front
door already has a pairing code, a store and a cookie (`auth.py`), so
sessionphone's Keyring and enrol code are not carried over: here the
pairing code is the enrol step.

The browser hands over the new credential's public key itself
(`getPublicKey()`, a SubjectPublicKeyInfo), so no CBOR is parsed.
Attestation is "none". Every ceremony needs user verification (Touch ID,
Face ID or a PIN), and one made inside a frame is refused.

A passkey belongs to one relying party (RP), a host name: the name the
front door is reached by, over https. WebAuthn refuses an IP address, so
the bare `127.0.0.1` door has no passkey.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

#: authenticatorData flags: user present, user verified.
UP, UV = 0x01, 0x04
#: COSE algorithms: ECDSA P-256 with SHA-256, RSASSA-PKCS1-v1_5 with SHA-256.
ES256, RS256 = -7, -257
#: The longest label a passkey keeps (the browser's guess at the device).
NAME_LEN = 80
#: The smallest RSA key an RS256 passkey may have, in bits.
RSA_MIN_BITS = 2048


def clean_label(text: object) -> str:
    """A label as the browser sent it, kept printable: control and other
    non-printing characters dropped, at most NAME_LEN characters. It is
    shown in a terminal (`session web passkeys`)."""
    if not isinstance(text, str):
        return ""
    return "".join(c for c in text if c.isprintable())[:NAME_LEN]


def b64u(b: bytes) -> str:
    """Bytes as unpadded base64url, the way WebAuthn spells them."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    """Unpadded base64url back to bytes; TypeError or ValueError when it is not."""
    if not isinstance(s, str):
        raise TypeError("not a string")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def rp_of(origin: str) -> str:
    """The RP id an origin's passkeys belong to: its host name."""
    return urlsplit(origin).hostname or ""


def _client_data(cdj: bytes, kind: str, chal: bytes | None, origin: str) -> str:
    """'' when clientDataJSON is right for this ceremony, else why not."""
    try:
        cd = json.loads(cdj)
    except ValueError:
        return "clientDataJSON is not JSON"
    if not isinstance(cd, dict):
        return "clientDataJSON is not an object"
    if cd.get("type") != kind:
        return f"type {cd.get('type')!r}, wanted {kind!r}"
    try:
        theirs = unb64u(str(cd.get("challenge", "")))
    except ValueError:
        return "challenge does not match"
    if not chal or not hmac.compare_digest(theirs, chal):
        return "challenge does not match"
    if cd.get("origin") != origin:
        return f"origin {cd.get('origin')!r}, wanted {origin!r}"
    # The front door sends frame-ancestors 'none'; a ceremony that ran inside
    # another origin's frame is refused, and anything but a boolean fails closed.
    cross = cd.get("crossOrigin")
    if cross is True:
        return "crossOrigin is true: signed in inside a frame"
    if cross is not None and cross is not False:
        return f"crossOrigin {cross!r} is not a boolean"
    return ""


def _auth_data(ad: bytes, rp_id: str) -> str:
    """'' when authenticatorData is for RP_ID, with presence and verification."""
    if len(ad) < 37:
        return "authenticatorData too short"
    if ad[:32] != hashlib.sha256(rp_id.encode()).digest():
        return f"rpIdHash is not {rp_id}'s"
    if not (ad[32] & UP and ad[32] & UV):
        return "no user presence and verification (Touch ID, Face ID or PIN)"
    return ""


def register(msg: dict, chal: bytes | None, origin: str) -> tuple[dict | None, str]:
    """(credential, '') when the new passkey checks out, else (None, why).
    The credential is what `auth.Store.add_passkey` keeps: no secret."""
    rp_id = rp_of(origin)
    try:
        cdj, ad = unb64u(msg["clientDataJSON"]), unb64u(msg["authenticatorData"])
        spki, alg, cid = unb64u(msg["publicKey"]), int(msg["alg"]), msg["id"]
    except (KeyError, TypeError, ValueError) as x:
        return None, f"malformed: {type(x).__name__}"
    why = _client_data(cdj, "webauthn.create", chal, origin) or _auth_data(ad, rp_id)
    if why:
        return None, why
    if alg not in (ES256, RS256):
        return None, f"algorithm {alg} not supported"
    try:
        pub = serialization.load_der_public_key(spki)
    except (ValueError, TypeError):
        return None, "public key does not load"
    why = _key_fits(pub, alg)
    if why:
        return None, why
    if not isinstance(cid, str) or not cid:
        return None, "malformed: no id"
    try:
        unb64u(cid)
    except ValueError:
        return None, "malformed: the id is not base64url"
    return {"id": cid, "rp_id": rp_id, "spki": b64u(spki), "alg": alg,
            "added": int(time.time()), "used": 0, "name": clean_label(msg.get("name"))}, ""


def _key_fits(pub: object, alg: int) -> str:
    """'' when PUB is a key ALG may use: ES256 a P-256 key, RS256 an RSA key
    of at least RSA_MIN_BITS; else why not."""
    if alg == ES256:
        if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(pub.curve,
                                                                           ec.SECP256R1):
            return "public key is not a P-256 key"
        return ""
    if not isinstance(pub, rsa.RSAPublicKey):
        return "public key is not an RSA key"
    if pub.key_size < RSA_MIN_BITS:
        return f"RSA key of {pub.key_size} bits, under {RSA_MIN_BITS}"
    return ""


def login(msg: dict, chal: bytes | None, origin: str, creds: list[dict]) -> str:
    """'' when the assertion is good for one of CREDS, else why not."""
    rp_id = rp_of(origin)
    try:
        cdj, ad = unb64u(msg["clientDataJSON"]), unb64u(msg["authenticatorData"])
        sig, cid = unb64u(msg["signature"]), msg["id"]
    except (KeyError, TypeError, ValueError) as x:
        return f"malformed: {type(x).__name__}"
    cred = next((c for c in creds if c.get("id") == cid and c.get("rp_id") == rp_id), None)
    if cred is None:
        return "unknown passkey"
    why = _client_data(cdj, "webauthn.get", chal, origin) or _auth_data(ad, rp_id)
    if why:
        return why
    try:
        pub = serialization.load_der_public_key(unb64u(cred["spki"]))
    except (KeyError, TypeError, ValueError):
        return "the kept public key does not load"
    if cred.get("alg") not in (ES256, RS256) or _key_fits(pub, cred["alg"]):
        return "the kept key does not match its algorithm"
    signed = ad + hashlib.sha256(cdj).digest()
    try:
        if cred["alg"] == ES256:
            pub.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        else:
            pub.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, TypeError, ValueError):
        return "signature does not verify"
    return ""
