"""
Tests de rate_limit.py. Igual que test_search_engine.py con Meilisearch:
acá solo va la parte que no necesita el servicio externo corriendo
(lectura de la variable de entorno). La verificación end-to-end contra un
Redis real (INCR, TTL, que se bloquee al superar el límite, que el modo
fail-open funcione si Redis no responde, y que los headers X-RateLimit-*/
Retry-After lleguen de verdad al cliente HTTP incluso en la respuesta 429)
se hizo a mano contra una instancia real — ver README, sección "Rate
limiting". No quedó en la suite automática por la misma razón que
Meilisearch: encadenar Postgres + Meilisearch + Redis en cada corrida de
tests es infraestructura que no se justifica todavía para este proyecto.
"""
import pytest

from graphql_api import rate_limit


def test_habilitado_responde_a_env_var_sin_reimportar(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert rate_limit.habilitado() is False
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    assert rate_limit.habilitado() is True


@pytest.mark.asyncio
async def test_verificar_y_consumir_sin_redis_url_deja_pasar(monkeypatch):
    """Sin Redis configurado, el rate limiting queda deshabilitado — todo
    pasa, y se devuelve el límite completo como 'restantes' (no hay nada
    que descontar todavía)."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    permitido, restantes, segundos_para_reset = await rate_limit.verificar_y_consumir(
        api_key_id=1, limite_por_minuto=10
    )
    assert permitido is True
    assert restantes == 10
    assert segundos_para_reset == rate_limit.VENTANA_SEGUNDOS
