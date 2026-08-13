"""
Prueba end-to-end del límite de profundidad por API key (ver
graphql_api/extensions.py): la MISMA query GraphQL anidada, contra el
schema público real montado en FastAPI, con dos API keys de límite
distinto. Complementa a test_extensions.py (que solo cubre la función
pura `_resolver_limite`, sin Strawberry real) con la integración
completa: que Strawberry efectivamente arme el validador con el número
correcto y lo aplique durante la validación — no solo que la lógica de
selección del número esté bien.

No depende de que exista ningún hecho_judicial real: el límite de
profundidad se evalúa en la fase de VALIDACIÓN de la query (contra su
estructura de AST), antes de que corra ningún resolver — así que la
consulta con código inexistente igual sirve para probar esto sin
necesitar datos de fixture.

Usa la app real (`graphql_api.app:app`) vía TestClient. No requiere
Meilisearch ni Redis (si no están seteados, quedan deshabilitados con su
fallback normal).
"""
import pytest
from fastapi.testclient import TestClient

from graphql_api.app import app

# Profundidad 4: Query -> hechoJudicial(1) -> categoriaDelito(2) ->
# categoriaPadre(3) -> nombre(4)
QUERY_PROFUNDIDAD_4 = """
query {
  hechoJudicial(codigo: "HJ-000001") {
    categoriaDelito {
      categoriaPadre {
        nombre
      }
    }
  }
}
"""


@pytest.mark.asyncio
async def test_el_limite_de_profundidad_es_por_api_key_no_global(api_key_temporal):
    key_permisiva = await api_key_temporal(limite_profundidad_query=10)
    key_estricta = await api_key_temporal(limite_profundidad_query=2)

    with TestClient(app) as client:
        r_permisiva = client.post(
            "/api/publico/graphql", json={"query": QUERY_PROFUNDIDAD_4}, headers={"X-API-Key": key_permisiva}
        )
        r_estricta = client.post(
            "/api/publico/graphql", json={"query": QUERY_PROFUNDIDAD_4}, headers={"X-API-Key": key_estricta}
        )

    # GraphQL: los errores de validación viajan en el body, no en el status HTTP.
    assert r_permisiva.status_code == 200
    assert r_permisiva.json().get("errors") is None, r_permisiva.json()

    assert r_estricta.status_code == 200
    errores = r_estricta.json().get("errors") or []
    assert errores, "la query de profundidad 4 debería haber sido rechazada por la key con límite 2"
    assert any("exceeds maximum operation depth of 2" in e["message"] for e in errores), errores
