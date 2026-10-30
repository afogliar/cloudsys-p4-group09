import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import DEVICE_TOKENS

bearer = HTTPBearer(auto_error=False)


def authenticate_device(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer),
    ],
) -> str:
    if credentials is not None:
        supplied_token = credentials.credentials.encode("utf-8")

        for device_id, expected_token in DEVICE_TOKENS.items():
            if secrets.compare_digest(
                supplied_token,
                expected_token.encode("utf-8"),
            ):
                return device_id

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Jeton absent ou invalide",
        headers={"WWW-Authenticate": "Bearer"},
    )