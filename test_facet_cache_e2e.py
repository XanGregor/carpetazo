"""
Prueba end-to-end de facet_cache.py contra un Redis real: que el valor
efectivamente quede guardado en Redis (no solo que la función "no
falle"), con el TTL pedido, y que una segunda llamada lo lea desde ahí en
vez de recalcular. La parte que no necesita Redis (env var, cache en
memoria) está en test_facet_cache.py.

Se salta automáticamente si no hay Redis disponible — ver docker-compose.yml.
"""
import json

import pytest

from graphql_api import facet_cache

pytestmark = pytest.mark.skipif(
    not facet_cache.habilitado_redis(),
    reason="Requiere REDIS_URL configurada y Redis corriendo — ver docker-compose.yml.",
)


@pytest.mark.asyncio
async def test_el_valor_queda_en_redis_con_el_ttl_pedido():
    cliente = facet_cache._obtener_cliente_redis()
    clave = "test-e2e-facetas-unica"
    await cliente.delete(f"{facet_cache.PREFIJO_CLAVE}:{clave}")

    llamadas = 0

    async def calcular():
        nonlocal llamadas
        llamadas += 1
        return {"categorias_delito": [{"id": 1, "nombre": "Test", "cantidad": 5}], "total": 5}

    resultado = await facet_cache.obtener_o_calcular(clave, calcular, ttl_segundos=30)
    assert llamadas == 1
    assert resultado["total"] == 5

    # El valor tiene que estar en Redis de verdad, no solo "no explotó".
    crudo = await cliente.get(f"{facet_cache.PREFIJO_CLAVE}:{clave}")
    assert crudo is not None
    assert json.loads(crudo) == resultado

    ttl = await cliente.ttl(f"{facet_cache.PREFIJO_CLAVE}:{clave}")
    assert 0 < ttl <= 30

    # Segunda llamada: debe leer de Redis, no recalcular.
    await facet_cache.obtener_o_calcular(clave, calcular, ttl_segundos=30)
    assert llamadas == 1

    await cliente.delete(f"{facet_cache.PREFIJO_CLAVE}:{clave}")
