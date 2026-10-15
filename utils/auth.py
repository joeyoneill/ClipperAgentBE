# Auth Dependency Functionality

# Imports
import os
import time
from fastapi import Depends, HTTPException, Query, status, WebSocket, WebSocketException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google.oauth2 import id_token
from google.auth.transport import requests
from pydantic import BaseModel

from db import db

# Init needed security objects
security = HTTPBearer()
request_adapter = requests.Request()

# Firestore Allowlist Config (`app_config/allowlist` -> {"enabled": true, "emails": [...]})
ALLOWLIST_COLLECTION_NAME = os.getenv("ALLOWLIST_COLLECTION_NAME", "app_config")
ALLOWLIST_DOC_ID = os.getenv("ALLOWLIST_DOC_ID", "allowlist")
ALLOWLIST_CACHE_TTL_SEC = float(os.getenv("ALLOWLIST_CACHE_TTL_SEC", "60"))

_allowlist_cache_expires_at: float = 0.0
_allowlist_cached_emails: set[str] | None = None


# Authenticated User Return Object
class UserInfo(BaseModel):
    uid: str
    email: str | None = None
    claims: dict


################################################################
# Firestore Email Allowlist Helpers (60s In-Memory Cache)
################################################################

def clear_allowlist_cache() -> None:
    """Resets the in-memory allowlist cache (used by tests or admin refreshes)."""
    global _allowlist_cache_expires_at, _allowlist_cached_emails
    _allowlist_cache_expires_at = 0.0
    _allowlist_cached_emails = None


def get_allowed_emails() -> set[str] | None:
    """
    Returns a lowercase set of allowed email addresses from Firestore
    (`app_config/allowlist` -> `emails: list[str]`), cached for 60 seconds.
    Returns `None` if no allowlist document or `ALLOWED_EMAILS` env var is configured.
    """
    global _allowlist_cache_expires_at, _allowlist_cached_emails

    now = time.monotonic()
    if now < _allowlist_cache_expires_at:
        return _allowlist_cached_emails

    env_emails = {
        e.strip().lower()
        for e in os.getenv("ALLOWED_EMAILS", "").split(",")
        if e.strip()
    }

    firestore_emails: set[str] | None = None
    try:
        doc_snap = (
            db.collection(ALLOWLIST_COLLECTION_NAME)
            .document(ALLOWLIST_DOC_ID)
            .get()
        )
        if getattr(doc_snap, "exists", False):
            data = doc_snap.to_dict()
            if isinstance(data, dict) and data.get("enabled", True) is not False:
                raw_list = data.get("emails")
                if isinstance(raw_list, list):
                    firestore_emails = {
                        str(e).strip().lower()
                        for e in raw_list
                        if str(e).strip()
                    }
    except Exception:
        # Fall back to env_emails if Firestore is temporarily unreachable
        pass

    if firestore_emails is not None or env_emails:
        merged = (firestore_emails or set()) | env_emails
        _allowlist_cached_emails = merged
    else:
        _allowlist_cached_emails = None

    _allowlist_cache_expires_at = now + ALLOWLIST_CACHE_TTL_SEC
    return _allowlist_cached_emails


################################################################
# Auth Util Functions
################################################################

def verify_firebase_jwt(token: str) -> UserInfo:
    """Shared JWT verification + Firestore email allowlist check for HTTP & WebSocket."""
    claims = id_token.verify_firebase_token(
        token,
        request_adapter,
        audience=os.environ["GCP_PROJECT_ID"],
    )
    uid = claims.get("sub") or claims.get("user_id")
    if not uid:
        raise ValueError("Token payload missing user identifier (sub/user_id).")

    email = claims.get("email")
    allowed_emails = get_allowed_emails()
    if allowed_emails is not None:
        normalized_email = (email or "").strip().lower()
        if not normalized_email or normalized_email not in allowed_emails:
            raise PermissionError(
                f"Access denied: '{email or 'unknown'}' is not on the authorized email allowlist."
            )

    return UserInfo(
        uid=uid,
        email=email,
        claims=dict(claims),
    )


# HTTP Auth Dependency Function
def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserInfo:
    try:
        return verify_firebase_jwt(credentials.credentials)
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# WebSocket Auth Helper (?token=<jwt>)
async def get_ws_current_user(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> UserInfo:
    if not token:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Missing authentication token query parameter.",
        )
    try:
        return verify_firebase_jwt(token)
    except Exception as e:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=f"Token verification failed: {str(e)}",
        )
