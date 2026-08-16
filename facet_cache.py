"""
Cache de corta duración (TTL de pocos segundos) para los conteos de
facetas del fallback de Postgres — `db.contar_facetas_hecho_judicial` /
`contar_facetas_declaracion`, usadas por `queries.py` únicamente cuando
Meilisearch NO está configurado. Con Meilisearch, `facetDistribution` ya
viene nativo del motor (ver search_engine.py) y este cache no aplica.

Por qué hace falta: cada búsqueda sin Meilisearch dispara, además de la
query principal, tres o dos `GROUP BY` más (uno por faceta) — bajo
volumen alto eso es carga extra evitable, porque los conteos de faceta no
cambian salvo que se publique/edite contenido, y ni siquiera hace falta
que se reflejen al instante: la propia UI tipo Letterboxd que motivó este
diseño ya tolera "conteos que se actualizan cada tanto", no en tiempo
real estricto.

Backend: Redis si está configurado (`REDIS_URL`, la misma variable que ya
usa rate_limit.py) — así el cache se comparte entre todos los
workers/réplicas de la app, que es donde más rinde bajo volumen real. Sin
Redis, cae a un cache en memoria del proceso — sirve igual para un solo
worker o desarrollo local, aunque cada proceso mantenga el suyo (no hay
nada compartido entre workers sin Redis, pero tampoco se pierde nada:
sigue siendo estrictamente mejor que no cachear).

Fail-open ante fallos de Redis: un error leyendo/escribiendo el cache
nunca debe impedir servir la búsqueda — mismo criterio que ya se usa para
Meilisearch/rate limiting en el resto del proyecto (ver search_engine.py /
rate_limit.py): se loguea y se sigue como si no hubiera cache
(recalculando contra Postgres).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)

TTL_SEGUNDOS_DEFAULT = 5
PREFIJO_CLAVE = "facetas"

_cliente_redis: Optional["redis.Redis"] = None
# clave -> (momento_de_expiracion_monotonic, valor) — solo se usa cuando
# no hay Redis configurado.
_cache_memoria: dict[str, tuple[float, dict]] = {}


def _url() -> Optional[str]:
    return os.environ.get("REDIS_URL")


def habilitado_redis() -> bool:
    return bool(_url())


def _obtener_cliente_redis() -> "redis.Redis":
    global _cliente_redis
    if _cliente_redis is None:
        _cliente_redis = redis.from_url(_url(), decode_responses=True)
    return _cliente_redis


async def cerrar_cliente() -> None:
    """Llamar en el shutdown de la app (ver app.py), igual que rate_limit.cerrar_cliente()."""
    global _cliente_redis
    cliente, _cliente_redis = _cliente_redis, None
    if cliente is None:
        return
    try:
        await cliente.aclose()
    except RuntimeError:
        # El event loop al que quedó atado este cliente (en su primer uso
        # real) ya está cerrado — típico en tests, donde cada test de
        # pytest-asyncio corre en su propio event loop y este cliente se
        # creó en uno de un test anterior. No hay nada más que cerrar
        # limpiamente en ese caso: las conexiones ya murieron junto con
        # ese loop, así que se ignora en vez de tumbar el shutdown.
        pass


def limpiar_cache_memoria() -> None:
    """Uso de test: vaciar el cache en memoria entre corridas para no
    arrastrar estado de un test a otro."""
    _cache_memoria.clear()


async def obtener_o_calcular(
    clave: str,
    calcular: Callable[[], Awaitable[dict]],
    *,
    ttl_segundos: int = TTL_SEGUNDOS_DEFAULT,
) -> dict:
    """
    Devuelve el valor cacheado bajo `clave` si todavía está fresco (menos
    de `ttl_segundos` desde que se calculó); si no, llama a `calcular()`,
    guarda el resultado con ese TTL, y lo devuelve.

    `calcular` tiene que devolver un dict JSON-serializable — las
    funciones contar_facetas_* de db.py ya devuelven dicts planos (no
    asyncpg.Record) justamente para que esto funcione sin conversión
    extra acá.
    """
    clave_completa = f"{PREFIJO_CLAVE}:{clave}"

    if habilitado_redis():
        try:
            cliente = _obtener_cliente_redis()
            crudo = await cliente.get(clave_completa)
            if crudo is not None:
                return json.loads(crudo)
        except (redis.RedisError, OSError, json.JSONDecodeError):
            logger.warning(
                "No se pudo leer el cache de facetas desde Redis (clave=%s) — se recalcula contra Postgres.",
                clave_completa,
            )
    else:
        entrada = _cache_memoria.get(clave_completa)
        if entrada is not None:
            expira_en, valor = entrada
            if expira_en > time.monotonic():
                return valor
            del _cache_memoria[clave_completa]

    valor = await calcular()

    if habilitado_redis():
        try:
            cliente = _obtener_cliente_redis()
            await cliente.set(clave_completa, json.dumps(valor), ex=ttl_segundos)
        except (redis.RedisError, OSError):
            logger.warning(
                "No se pudo escribir el cache de facetas en Redis (clave=%s) — se sigue sin cachear esta entrada.",
                clave_completa,
            )
    else:
        _cache_memoria[clave_completa] = (time.monotonic() + ttl_segundos, valor)

    return valor
