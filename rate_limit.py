"""
Rate limiting por API key para la superficie pública (ver context.py).

Implementa lo que quedaba pendiente en el README: hacer cumplir de verdad
el `rate_limit_por_minuto` que ya se guarda por fila en la tabla `api_key`
(hasta ahora solo se guardaba, nada lo hacía cumplir). Es lo que sostiene
la decisión de registro automático sin aprobación manual para la API key
pública (ver solicitar_api_key en mutations.py): el control de abuso no
pasa por filtrar quién se registra, pasa por acá.

Por qué Redis y no un contador en memoria del proceso: un contador en
memoria solo sirve si hay un único proceso de Python corriendo. En cuanto
FastAPI/uvicorn corre con más de un worker (lo normal en producción), cada
worker tendría su propio contador y el límite real terminaría siendo
rate_limit_por_minuto × cantidad_de_workers — el límite dejaría de
significar lo que dice. Redis es el contador compartido entre workers.

Algoritmo: ventana fija de 60s alineada al reloj (no al primer request de
cada cliente) — la clave incluye floor(epoch / 60), así todas las API keys
comparten el mismo punto de corte de minuto. Es la implementación más
simple que cumple lo que pide "límite por minuto", y fácil de inspeccionar
a mano en Redis. Trade-off conocido y aceptado como punto de partida (en
la misma línea que los límites fijos de profundidad/alias/tokens ya
definidos en schema.py): un cliente puede rozar ~2x el límite si concentra
requests justo en el borde entre dos minutos consecutivos. Si en
producción se ve abuso real aprovechando ese borde, se puede pasar a un
sliding window sin cambiar la firma de verificar_y_consumir.

Sin REDIS_URL: el rate limiting queda deshabilitado (todo pasa) y se
loguea un warning una sola vez al arrancar — mismo patrón que
search_engine.habilitado() para Meilisearch, para no forzar a levantar
Redis también solo para desarrollar localmente.

Si Redis está configurado pero no responde en el momento de un request
puntual: se deja pasar el request (fail-open) en vez de tumbar la API
pública entera por la caída de un sistema secundario — mismo criterio que
ya se usa con Meilisearch (ver search_engine.py: un fallo ahí se loguea y
la respuesta sigue, nunca se le niega al usuario por eso). Queda logueado
con logger.error para que se note y se pueda actuar. Si se prefiere el
criterio inverso — negar el request si Redis no responde, priorizando el
control de abuso por sobre la disponibilidad — alcanza con invertir el
`return True` del except de abajo.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)

VENTANA_SEGUNDOS = 60

_cliente: Optional["redis.Redis"] = None
_avisado_deshabilitado = False


def _url() -> Optional[str]:
    return os.environ.get("REDIS_URL")


def habilitado() -> bool:
    return bool(_url())


def _obtener_cliente() -> "redis.Redis":
    global _cliente
    if _cliente is None:
        _cliente = redis.from_url(_url(), decode_responses=True)
    return _cliente


async def cerrar_cliente() -> None:
    """Llamar en el shutdown de la app (ver app.py) para cerrar la conexión prolijamente."""
    global _cliente
    if _cliente is not None:
        await _cliente.close()
        _cliente = None


async def verificar_y_consumir(api_key_id: int, limite_por_minuto: int) -> tuple[bool, int, int]:
    """
    Cuenta un request más para esta API key en la ventana de 60s actual.

    Devuelve (permitido, restantes, segundos_para_reset) — pensado para
    que quien llama pueda tanto decidir si bloquea el request como devolver
    los headers X-RateLimit-* / Retry-After (ver context.py).
    """
    if not habilitado():
        global _avisado_deshabilitado
        if not _avisado_deshabilitado:
            logger.warning("REDIS_URL no está configurada — rate limiting deshabilitado en la API pública.")
            _avisado_deshabilitado = True
        return True, limite_por_minuto, VENTANA_SEGUNDOS

    ahora = time.time()
    ventana_actual = int(ahora // VENTANA_SEGUNDOS)
    segundos_para_reset = VENTANA_SEGUNDOS - int(ahora % VENTANA_SEGUNDOS)
    clave = f"ratelimit:api_key:{api_key_id}:{ventana_actual}"

    try:
        cliente = _obtener_cliente()
        conteo = await cliente.incr(clave)
        if conteo == 1:
            # Solo el primer request de la ventana pone el TTL — si se
            # reseteara en cada request, una key muy activa podría
            # renovarlo indefinidamente y la clave nunca expiraría.
            await cliente.expire(clave, VENTANA_SEGUNDOS + 5)
    except (redis.RedisError, OSError):
        logger.error(
            "Redis no respondió al verificar el rate limit de api_key_id=%s — se deja pasar el request.",
            api_key_id,
        )
        return True, limite_por_minuto, segundos_para_reset

    restantes = limite_por_minuto - conteo
    return conteo <= limite_por_minuto, restantes, segundos_para_reset
