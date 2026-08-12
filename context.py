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

contexto_publico además hace cumplir el rate limit por API key (ver
rate_limit.py) antes de dejar pasar el request hacia GraphQL — el
parámetro `response: Response` no es cosmético: FastAPI comparte la misma
instancia de Response entre todas las dependencias de un mismo request, así
que los headers que se setean acá (X-RateLimit-*, Retry-After) llegan de
verdad a la respuesta HTTP final, aunque este dependency-getter termine
antes de que Strawberry arme el cuerpo de la respuesta.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Header, HTTPException, Request, Response

from . import auth, db, rate_limit
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
    response: Response,
    x_api_key: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Falta el header X-API-Key.")
    pool = await db.get_pool()
    async with pool.acquire() as con:
        api_key = await auth.validar_api_key(con, x_api_key)
    if api_key is None:
        raise HTTPException(status_code=401, detail="API key inválida o desactivada.")

    permitido, restantes, segundos_para_reset = await rate_limit.verificar_y_consumir(
        api_key.id, api_key.rate_limit_por_minuto
    )
    headers_limite = {
        "X-RateLimit-Limit": str(api_key.rate_limit_por_minuto),
        "X-RateLimit-Remaining": str(max(restantes, 0)),
        "X-RateLimit-Reset": str(segundos_para_reset),
    }
    if not permitido:
        # OJO: acá no alcanza con mutar response.headers como en el caso de
        # éxito de abajo — cuando se levanta una HTTPException, Starlette
        # arma la respuesta de error DESDE CERO (ver
        # fastapi.exception_handlers.http_exception_handler) y no la
        # mezcla con el Response temporal de la dependencia. Los headers
        # tienen que ir explícitos en el `headers=` de la excepción, o se
        # pierden — se comprobó a mano (ver README) que mutar response acá
        # los descarta en el 429 aunque sí funcione en el 200.
        raise HTTPException(
            status_code=429,
            detail=f"Límite de {api_key.rate_limit_por_minuto} requests por minuto excedido para esta API key.",
            headers={**headers_limite, "Retry-After": str(segundos_para_reset)},
        )

    response.headers.update(headers_limite)
    return {
        "pool": pool,
        "api_key": api_key,
        "dataloaders": Loaders(pool),
    }
