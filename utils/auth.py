# Auth Dependency Functionality

# Imports
from fastapi import Depends, HTTPException, Query, status, WebSocket, WebSocketException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google.oauth2 import id_token
from google.auth.transport import requests
from pydantic import BaseModel
import os

# Init needed security objects
security = HTTPBearer()
request_adapter = requests.Request()

# Authenticated User Return Object
class UserInfo(BaseModel):
    uid: str
    email: str | None = None
    claims: dict

################################################################
# Auth Util Functions
################################################################

def verify_firebase_jwt(token: str) -> UserInfo:
    """Shared JWT verification logic for both HTTP and WebSocket endpoints."""
    claims = id_token.verify_firebase_token(
        token,
        request_adapter,
        audience=os.environ["GCP_PROJECT_ID"],
    )
    uid = claims.get("sub") or claims.get("user_id")
    if not uid:
        raise ValueError("Token payload missing user identifier (sub/user_id).")
    return UserInfo(
        uid=uid,
        email=claims.get("email"),
        claims=dict(claims),
    )


# HTTP Auth Dependency Function
def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserInfo:
    try:
        return verify_firebase_jwt(credentials.credentials)
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