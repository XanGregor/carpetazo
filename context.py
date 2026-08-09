"""
context_getter para los dos routers de FastAPI (ver schema.py).

Importante: el contexto expone el POOL de conexiones, no una conexión
compartida. GraphQL resuelve campos hermanos en paralelo (ej: al pedir
`categoriaDelito`, `provincia` y `fuentes` de un mismo hecho, las tres
relaciones se resuelven concurrentemente) — una sola conexión de asyncpg
no soporta queries concurrentes y tira "another operation is in progress"
si se comparte. Cada resolver/DataLoader saca su propia conexión del pool
con `pool.acquire()` y la devuelve al terminar (ver dataloaders.py,
queries.py y mutations.py).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Header, HTTPException, Request

from . import auth, db
from .dataloaders import Loaders


async def contexto_interno(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    pool = await db.get_pool()
    usuario = None
    if authorization and authorization.lower().startswith("bearer "):
        usuario = auth.decodificar_token(authorization[7:])
    return {
        "pool": pool,
        "usuario": usuario,
        "dataloaders": Loaders(pool),
    }


async def contexto_publico(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Falta el header X-API-Key.")
    pool = await db.get_pool()
    async with pool.acquire() as con:
        api_key = await auth.validar_api_key(con, x_api_key)
    if api_key is None:
        raise HTTPException(status_code=401, detail="API key inválida o desactivada.")
    return {
        "pool": pool,
        "api_key": api_key,
        "dataloaders": Loaders(pool),
    }
