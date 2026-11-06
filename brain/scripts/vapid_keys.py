"""Prints a new VAPID key pair for the Web Push settings. Writes nothing.

    brain/.venv/bin/python -m brain.scripts.vapid_keys

Copy the two lines into the env of the web app and the agents' worker (both need the same pair):
WEB_PUSH_PUBLIC_KEY goes to the browser, WEB_PUSH_PRIVATE_KEY is a secret. A new pair invalidates every
subscription made with the old one.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_pair() -> tuple[str, str]:
    """(public, private): the P-256 public point uncompressed (65 bytes) and the private number (32 bytes),
    base64url without padding, as the browser's applicationServerKey and pywebpush expect them."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    private = key.private_numbers().private_value.to_bytes(32, "big")
    return _b64url(public), _b64url(private)


def main() -> None:
    public, private = new_pair()
    print(f"WEB_PUSH_PUBLIC_KEY={public}")
    print(f"WEB_PUSH_PRIVATE_KEY={private}")


if __name__ == "__main__":
    main()
