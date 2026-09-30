# Auth Dependency Functionality

# Imports
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google.oauth2 import id_token
from google.auth.transport import requests
from pydantic import BaseModel, EmailStr
import os

# Init needed security objects
security = HTTPBearer()
request_adapter = requests.Request()

# Authenticated User Return Object
class UserInfo(BaseModel):
    uid: str
    email: str | None = None
    claims: dict

# Auth Dependency Function
def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> UserInfo | None:
    token = credentials.credentials
    try:
        # Get Claims from jwt
        claims = id_token.verify_firebase_token(
            token,
            request_adapter,
            audience=os.environ["GCP_PROJECT_ID"],
        )
        
        # rip user id
        uid = claims.get("sub") or claims.get("user_id")
        if not uid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token payload missing user identifier (sub/user_id).",
            )

        return UserInfo(
            uid=uid,
            email=claims.get("email"),
            claims=dict(claims),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )