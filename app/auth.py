"""Bearer API key check."""

import hmac


def extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def is_authorized(authorization: str | None, expected_key: str) -> bool:
    token = extract_bearer(authorization)
    if token is None:
        return False
    # Constant-time compare so response timing does not leak how much of the key matched.
    return hmac.compare_digest(token.encode(), expected_key.encode())
