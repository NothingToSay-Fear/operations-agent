import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request
from sqlalchemy import select

from app.models import Session, User


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password, encoded):
    _, salt, expected = encoded.split("$")
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(digest, expected)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


async def current_user(request: Request):
    token = request.cookies.get("ops_session", "")
    if not token:
        raise HTTPException(401, "请先登录")
    async with request.app.state.db.sessions() as session:
        user = await session.scalar(
            select(User)
            .join(Session)
            .where(Session.token_hash == token_hash(token), Session.expires_at > time.time())
        )
    if not user:
        raise HTTPException(401, "登录已失效，请重新登录")
    return user
