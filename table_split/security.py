"""
table_split.security — Cryptographic tokens, hashing, and room code generators.
"""

import hashlib
import hmac
import json
import secrets
from typing import Any

# Unambiguous alphanumeric characters (excluding 0, O, 1, I, L)
UNAMBIGUOUS_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_join_token_128() -> str:
    """
    Generates a 128-bit cryptographically secure URL-safe token.
    16 bytes = 128 bits of entropy.
    """
    return secrets.token_urlsafe(16)


def generate_guest_token() -> str:
    """
    Generates a secure guest token (32 bytes = 256 bits of entropy).
    Returned in plaintext to client once; stored hashed in DB.
    """
    return secrets.token_hex(32)


def hash_token(token: str) -> str:
    """
    Computes standard SHA-256 hex digest of a token.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(raw_token: str, expected_hash: str) -> bool:
    """
    Constant-time comparison against expected hash to prevent timing attacks.
    """
    candidate_hash = hash_token(raw_token)
    return hmac.compare_digest(candidate_hash, expected_hash)


def generate_room_code(prefix: str = "TAB", suffix_len: int = 4) -> str:
    """
    Generates a short human-readable code e.g. TAB-8K2M.
    Uses unambiguous characters to avoid diner confusion when reading codes aloud.
    """
    suffix = "".join(secrets.choice(UNAMBIGUOUS_CHARS) for _ in range(suffix_len))
    return f"{prefix}-{suffix}"


def hash_payload(data: Any) -> str:
    """
    Creates a deterministic SHA-256 hash of a payload dictionary or object for audit records.
    """
    if isinstance(data, (dict, list)):
        payload_bytes = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    else:
        payload_bytes = str(data).encode("utf-8")
    return hashlib.sha256(payload_bytes).hexdigest()
