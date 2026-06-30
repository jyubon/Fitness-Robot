"""
JWT authentication and password hashing for the Fitness Robot Web App.
Real users only — no dummy accounts.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from bson import ObjectId
import os

from .database import get_db
from .models import UserRegister, TokenResponse

# --- Configuration ---
SECRET_KEY = os.getenv("JWT_SECRET", "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET_IN_PRODUCTION")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def get_current_user_id(token: str = Depends(oauth2_scheme)) -> str:
    """
    Dependency that extracts and validates the JWT.
    Returns the user_id (str) from the token.
    Raises 401 on any failure.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: Optional[str] = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # Optional: verify user still exists in DB
    db = get_db()
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise credentials_exception

    return user_id


async def authenticate_user(email: str, password: str) -> Optional[dict]:
    """Return user document if email+password match, else None."""
    db = get_db()
    user = await db.users.find_one({"email": email.lower()})
    if not user:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


async def register_user(email: str, password: str) -> dict:
    """
    Create a new user. Raises ValueError if email already exists.
    Returns the created user document (without password).
    """
    db = get_db()
    existing = await db.users.find_one({"email": email.lower()})
    if existing:
        raise ValueError("Email already registered")

    hashed = get_password_hash(password)
    now = datetime.now(timezone.utc)
    user_doc = {
        "email": email.lower(),
        "password_hash": hashed,
        "created_at": now,
        "preferences": {},
    }
    result = await db.users.insert_one(user_doc)
    user_doc["_id"] = result.inserted_id
    # Return safe representation
    return {
        "id": str(result.inserted_id),
        "email": user_doc["email"],
        "created_at": user_doc["created_at"],
    }


async def login_user(email: str, password: str) -> TokenResponse:
    """Authenticate and return JWT + user_id."""
    user = await authenticate_user(email, password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = str(user["_id"])
    access_token = create_access_token(data={"sub": user_id})
    return TokenResponse(access_token=access_token, user_id=user_id)
