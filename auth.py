"""
Autenticación. Dos mecanismos distintos, uno por superficie de API:

  - Interna (web/apps propias, con mutaciones): JWT firmado, emitido por la
    mutación iniciarSesion. Se manda en el header Authorization: Bearer <token>.
  - Pública (terceros, solo lectura): API key emitida al registrarse (ver
    SolicitarApiKeyInput). Se manda en el header X-API-Key. Se guarda
    hasheada en la base — igual que una contraseña, nunca en texto plano.

Cambiar JWT_SECRET en producción vía variable de entorno; el valor acá es
solo para que el módulo no rompa si falta la env var en desarrollo local.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import bcrypt
import jwt

from .enums import RolUsuario

JWT_SECRET = os.environ.get("JWT_SECRET", "cambiar-en-produccion")
JWT_ALGORITHM = "HS256"
JWT_EXPIRA_HORAS = 12


@dataclass
class UsuarioActual:
    id: int
    email: str
    rol: RolUsuario


@dataclass
class ApiKeyActual:
    id: int
    nombre: str
    rate_limit_por_minuto: int
    limite_profundidad_query: int


# ---------------------------------------------------------------------------
# Contraseñas
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verificar_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# ---------------------------------------------------------------------------
# JWT (superficie interna)
# ---------------------------------------------------------------------------

def crear_token(usuario: UsuarioActual) -> str:
    payload = {
        "sub": str(usuario.id),
        "email": usuario.email,
        "rol": usuario.rol.value,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRA_HORAS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decodificar_token(token: str) -> Optional[UsuarioActual]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    return UsuarioActual(id=int(payload["sub"]), email=payload["email"], rol=RolUsuario(payload["rol"]))


async def autenticar(con: asyncpg.Connection, email: str, password: str) -> Optional[UsuarioActual]:
    fila = await con.fetchrow("SELECT * FROM usuario WHERE email = $1 AND activo = TRUE", email)
    if fila is None or not verificar_password(password, fila["password_hash"]):
        return None
    return UsuarioActual(id=fila["id"], email=fila["email"], rol=RolUsuario(fila["rol"]))


# ---------------------------------------------------------------------------
# API keys (superficie pública)
# ---------------------------------------------------------------------------

def generar_api_key() -> tuple[str, str]:
    """Devuelve (key_en_texto_plano_para_mostrar_una_vez, hash_para_guardar)."""
    key_plana = f"acp_{secrets.token_urlsafe(32)}"
    return key_plana, hashlib.sha256(key_plana.encode("utf-8")).hexdigest()


async def validar_api_key(con: asyncpg.Connection, key_plana: str) -> Optional[ApiKeyActual]:
    key_hash = hashlib.sha256(key_plana.encode("utf-8")).hexdigest()
    fila = await con.fetchrow("SELECT * FROM api_key WHERE key_hash = $1 AND activa = TRUE", key_hash)
    if fila is None:
        return None
    await con.execute("UPDATE api_key SET ultimo_uso = now() WHERE id = $1", fila["id"])
    return ApiKeyActual(
        id=fila["id"],
        nombre=fila["nombre"],
        rate_limit_por_minuto=fila["rate_limit_por_minuto"],
        limite_profundidad_query=fila["limite_profundidad_query"],
    )
