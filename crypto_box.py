"""
libsodium sealed boxes (PyNaCl) for ATS credentials at rest.

Asymmetric on purpose: the web service holds only ATS_CREDS_PUBLIC_KEY, so it
can WRITE credentials but can never read them back — even a compromised web
dyno can't leak stored passwords. Only the Playwright worker holds
ATS_CREDS_PRIVATE_KEY and can decrypt at fill time.

Generate a keypair once:
    python3 crypto_box.py keygen
then set ATS_CREDS_PUBLIC_KEY on the web service and BOTH vars on the worker.
"""

import os
import base64


def _keys():
    from nacl.public import PrivateKey, PublicKey
    pub = os.getenv("ATS_CREDS_PUBLIC_KEY", "")
    priv = os.getenv("ATS_CREDS_PRIVATE_KEY", "")
    return (
        PublicKey(base64.b64decode(pub)) if pub else None,
        PrivateKey(base64.b64decode(priv)) if priv else None,
    )


def seal(plaintext: str) -> str:
    """Encrypt with the public key. Anyone with the env var can seal."""
    from nacl.public import SealedBox
    pub, _ = _keys()
    if not pub:
        raise RuntimeError("ATS_CREDS_PUBLIC_KEY not set — run: python3 crypto_box.py keygen")
    return base64.b64encode(SealedBox(pub).encrypt(plaintext.encode())).decode()


def unseal(sealed_b64: str) -> str:
    """Decrypt with the private key. Worker-only."""
    from nacl.public import SealedBox
    _, priv = _keys()
    if not priv:
        raise RuntimeError("ATS_CREDS_PRIVATE_KEY not set on this service")
    return SealedBox(priv).decrypt(base64.b64decode(sealed_b64)).decode()


def keygen() -> dict:
    from nacl.public import PrivateKey
    priv = PrivateKey.generate()
    return {
        "ATS_CREDS_PUBLIC_KEY": base64.b64encode(bytes(priv.public_key)).decode(),
        "ATS_CREDS_PRIVATE_KEY": base64.b64encode(bytes(priv)).decode(),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "keygen":
        pair = keygen()
        print("# Add to your env (public on web service, both on worker):")
        for k, v in pair.items():
            print(f"{k}={v}")
    else:
        print("usage: python3 crypto_box.py keygen")
