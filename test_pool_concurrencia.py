"""
Smoke test de concurrencia para la suite normal de CI — distinto de
scripts/test_carga_pool.py (que es la herramienta de calibración manual,
más pesada y con reportes detallados de latencia). Este archivo es
deliberadamente chico y rápido: no calibra nada, solo confirma en cada
push que una ráfaga de requests concurrentes contra una ficha con fan-out
de varias relaciones (el escenario que motiva toda esta suite, ver
dataloaders.py) no rompe nada — ni error, ni excepción, ni el
"InterfaceError: another operation is in progress" que ya se corrigió una
vez (ver README, "Qué encontraron los tests", bug #1) y que sería fácil
reintroducir sin darse cuenta si alguien vuelve a compartir una conexión
entre resolvers en el futuro.

No mide latencia ni calibra pool sizing — para eso está
scripts/test_carga_pool.py, pensado para correrse a mano cuando haga
falta, no en cada push (es más lento y sus resultados dependen del
hardware de la corrida, no tiene sentido como gate de CI).
"""
import asyncio

import httpx
import pytest

from graphql_api import db
from graphql_api.app import app

QUERY_ANCHA = """
query {
  hechoJudicial(codigo: "HJ-000001") {
    titulo
    categoriaDelito { nombre }
    provincia { nombre }
    fuentes { nivel url }
    personas { rol { nombre } persona { nombreCompleto } }
    organizaciones { rol { nombre } organizacion { nombre } }
    relaciones { tipoRelacion descripcion }
  }
}
"""


@pytest.mark.asyncio
async def test_ráfaga_concurrente_de_query_ancha_no_rompe_nada():
    """
    20 requests concurrentes de la query con más fan-out (6 relaciones,
    6 conexiones del pool por request) — si alguna vez se reintroduce el
    bug de conexión compartida, esto lo va a agarrar rápido y con un
    mensaje de error específico, sin depender de correr el script de
    carga completo.

    Nota: se usa httpx.ASGITransport directo (no `with TestClient(app)`)
    para lanzar los 20 requests con asyncio.gather dentro del mismo event
    loop y lograr concurrencia real — pero eso significa que el lifespan
    de FastAPI (que abre/cierra la pool en db.py) NUNCA se dispara solo.
    Por eso hace falta cerrar la pool a mano al final: si no, queda
    abierta y filtra conexiones hacia los tests que corren después en la
    misma sesión de pytest (esto se detectó de verdad: sin el
    db.close_pool() de abajo, una corrida completa de la suite terminaba
    en TooManyConnectionsError más adelante).
    """
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            respuestas = await asyncio.gather(
                *(client.post("/graphql", json={"query": QUERY_ANCHA}) for _ in range(20))
            )
    finally:
        await db.close_pool()

    for r in respuestas:
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert cuerpo.get("errors") is None, cuerpo["errors"]
