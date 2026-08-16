"""
Tests de facet_cache.py. La parte que no necesita Redis corriendo (env
var, cache en memoria con TTL) va acá, mismo criterio que
test_search_engine.py/test_rate_limit.py. La integración real contra
Redis (que el TTL efectivamente expire, que dos procesos compartan el
cache) se cubre en test_facetas_fallback_e2e.py, que se salta si no hay
Redis disponible.
"""
import asyncio

import pytest

from graphql_api import facet_cache


@pytest.fixture(autouse=True)
def _limpiar_cache_memoria():
    facet_cache.limpiar_cache_memoria()
    yield
    facet_cache.limpiar_cache_memoria()


def test_habilitado_redis_responde_a_env_var_sin_reimportar(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert facet_cache.habilitado_redis() is False
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    assert facet_cache.habilitado_redis() is True


@pytest.mark.asyncio
async def test_cache_en_memoria_evita_un_segundo_calculo_dentro_del_ttl(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)  # fuerza el camino de memoria

    llamadas = 0

    async def calcular():
        nonlocal llamadas
        llamadas += 1
        return {"valor": llamadas}

    primero = await facet_cache.obtener_o_calcular("clave-test", calcular, ttl_segundos=60)
    segundo = await facet_cache.obtener_o_calcular("clave-test", calcular, ttl_segundos=60)

    assert llamadas == 1, "la segunda llamada debería haber usado el cache, no recalculado"
    assert primero == segundo == {"valor": 1}


@pytest.mark.asyncio
async def test_cache_en_memoria_recalcula_despues_de_que_expira_el_ttl(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)

    llamadas = 0

    async def calcular():
        nonlocal llamadas
        llamadas += 1
        return {"valor": llamadas}

    await facet_cache.obtener_o_calcular("clave-ttl-corto", calcular, ttl_segundos=0.05)
    await asyncio.sleep(0.1)
    await facet_cache.obtener_o_calcular("clave-ttl-corto", calcular, ttl_segundos=0.05)

    assert llamadas == 2, "después de expirar el TTL, la siguiente llamada debería recalcular"


@pytest.mark.asyncio
async def test_claves_distintas_no_comparten_cache(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)

    llamadas = 0

    async def calcular():
        nonlocal llamadas
        llamadas += 1
        return {"valor": llamadas}

    await facet_cache.obtener_o_calcular("clave-a", calcular, ttl_segundos=60)
    await facet_cache.obtener_o_calcular("clave-b", calcular, ttl_segundos=60)

    assert llamadas == 2
