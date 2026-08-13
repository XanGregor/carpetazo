"""
Prueba end-to-end de rate_limit.py contra un Redis real: que bloquee
exactamente al superar el límite y no antes, y que los headers
X-RateLimit-*/Retry-After lleguen de verdad al cliente HTTP tanto en el
`200` como en el `429` — el hallazgo real documentado en el README sobre
`HTTPException` y headers se detectó probando esto mismo a mano; queda
automatizado acá para que no se repita en silencio si alguien toca
context.py más adelante.

Usa la app real (`graphql_api.app:app`) vía TestClient, no una app de
juguete — así se prueba la integración real (contexto_publico + Strawberry
GraphQLRouter), no una aproximación.

Se salta automáticamente si no hay Redis disponible — ver docker-compose.yml.
"""
import time

import pytest
from fastapi.testclient import TestClient

from graphql_api import rate_limit
from graphql_api.app import app

pytestmark = pytest.mark.skipif(
    not rate_limit.habilitado(),
    reason="Requiere REDIS_URL configurada y Redis corriendo — ver docker-compose.yml.",
)

QUERY_TRIVIAL = "query { __typename }"


async def _esperar_si_esta_por_cerrar_la_ventana() -> None:
    """
    La ventana de rate_limit.py es fija y alineada al reloj (floor(epoch/60),
    ver rate_limit.py). Si el test arranca a milisegundos de que cierre la
    ventana, el burst de requests de abajo podría partirse en dos ventanas
    distintas y dar un conteo que no es el esperado — no es un bug del rate
    limiter (es el trade-off de ventana fija ya documentado y aceptado),
    pero sí haría flaky a ESTE test puntual. Se evita esperando a estar
    cómodamente lejos del borde antes de arrancar el burst.
    """
    segundos_para_reset = 60 - int(time.time() % 60)
    if segundos_para_reset < 10:
        import asyncio

        await asyncio.sleep(segundos_para_reset + 0.5)


@pytest.mark.asyncio
async def test_bloquea_al_superar_el_limite_y_los_headers_llegan_en_200_y_429(api_key_temporal):
    await _esperar_si_esta_por_cerrar_la_ventana()
    key = await api_key_temporal(rate_limit_por_minuto=3)

    with TestClient(app) as client:
        respuestas = [
            client.post("/api/publico/graphql", json={"query": QUERY_TRIVIAL}, headers={"X-API-Key": key})
            for _ in range(5)
        ]

    permitidas = respuestas[:3]
    bloqueadas = respuestas[3:]

    for i, r in enumerate(permitidas):
        assert r.status_code == 200, r.text
        assert r.headers["x-ratelimit-limit"] == "3"
        assert r.headers["x-ratelimit-remaining"] == str(2 - i)

    for r in bloqueadas:
        assert r.status_code == 429, r.text
        assert "retry-after" in {h.lower() for h in r.headers.keys()}
        assert r.headers["x-ratelimit-remaining"] == "0"
        assert "Límite" in r.json()["detail"]


@pytest.mark.asyncio
async def test_dos_api_keys_tienen_contadores_independientes(api_key_temporal):
    await _esperar_si_esta_por_cerrar_la_ventana()
    key_a = await api_key_temporal(rate_limit_por_minuto=1)
    key_b = await api_key_temporal(rate_limit_por_minuto=1)

    with TestClient(app) as client:
        r_a1 = client.post("/api/publico/graphql", json={"query": QUERY_TRIVIAL}, headers={"X-API-Key": key_a})
        r_b1 = client.post("/api/publico/graphql", json={"query": QUERY_TRIVIAL}, headers={"X-API-Key": key_b})
        r_a2 = client.post("/api/publico/graphql", json={"query": QUERY_TRIVIAL}, headers={"X-API-Key": key_a})

    assert r_a1.status_code == 200
    assert r_b1.status_code == 200, "una key distinta con su propio límite de 1 no debería verse afectada por key_a"
    assert r_a2.status_code == 429, "la segunda request de la MISMA key sí debe bloquearse"
