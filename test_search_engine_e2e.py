"""
Prueba end-to-end de search_engine.py contra un Meilisearch real: hasta
ahora esto se venía validando a mano en cada sesión de trabajo (ver
README) porque encadenar dos servicios en la suite automática no se
justificaba sin docker-compose. Ahora que docker-compose.yml levanta
Meilisearch junto con Postgres, este es el lugar natural para automatizar
esa prueba — los cinco casos que documentaba el README como "probado a
mano": indexar, buscar por texto, filtrar por faceta (incluyendo el
filtro por array `organizaciones_ids`), que `facetDistribution` tenga la
forma esperada, y que lo no-publicado nunca aparezca.

Usa `sincronizar_hecho_judicial` (la función real que llaman las
mutations), no los primitivos de bajo nivel — así se prueba el camino
completo Postgres -> vista denormalizada -> documento -> Meilisearch, no
solo la construcción del documento en aislado (eso ya lo cubre
test_search_engine.py sin necesitar el motor corriendo).

Se salta automáticamente si no hay Meilisearch disponible (MEILISEARCH_URL
sin setear) — no todos los entornos donde corre la suite van a tenerlo
levantado; ver docker-compose.yml para levantarlo.
"""
import asyncio

import pytest
import pytest_asyncio

from graphql_api import db, search_engine

pytestmark = pytest.mark.skipif(
    not search_engine.habilitado(),
    reason="Requiere MEILISEARCH_URL configurada y Meilisearch corriendo — ver docker-compose.yml.",
)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _configurar_indices():
    """Corre una vez para todo el módulo — es el mismo PATCH idempotente
    que hace app.py al arrancar, hace falta para que filtrar por faceta
    funcione (searchableAttributes/filterableAttributes)."""
    await search_engine.configurar_indices()


async def _esperar_hasta(condicion, *, timeout: float = 5.0, intervalo: float = 0.15):
    """
    Poll con timeout en vez de un sleep fijo: Meilisearch indexa como tarea
    interna asíncrona y cuánto tarda varía con la carga de la máquina que
    corre el test (se midió una tarda real de ~0.85s en el sandbox de
    desarrollo — un sleep fijo de 0.5s ya no alcanzaba). `condicion` es una
    corrutina sin argumentos que devuelve True cuando ya se puede seguir.
    """
    transcurrido = 0.0
    while transcurrido < timeout:
        if await condicion():
            return
        await asyncio.sleep(intervalo)
        transcurrido += intervalo
    raise AssertionError(f"la condición no se cumplió después de {timeout}s de polling")


@pytest.mark.asyncio
async def test_sincronizar_indexa_solo_lo_publicado_y_permite_filtrar_por_facetas(con, usuarios, ids_semilla):
    publicado = await db.insertar_hecho_judicial(
        con, titulo="E2E Meilisearch publicado", descripcion="Documento de prueba end-to-end",
        categoria_delito_id=ids_semilla["categoria_delito_id"], estado_judicial="procesado",
        fecha_hecho=None, provincia_id=ids_semilla["provincia_id"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )
    pendiente = await db.insertar_hecho_judicial(
        con, titulo="E2E Meilisearch pendiente — no debe indexarse", descripcion="...",
        categoria_delito_id=ids_semilla["categoria_delito_id"], estado_judicial="denuncia",
        fecha_hecho=None, provincia_id=ids_semilla["provincia_id"],
        estado_publicacion="pendiente_aprobacion", creado_por=usuarios["admin"].id, aprobado_por=None,
    )
    org = await con.fetchrow(
        "INSERT INTO organizacion (nombre, tipo) VALUES ('Org E2E Meilisearch', 'partido_politico') RETURNING id"
    )
    await db.insertar_vinculo_organizacion(con, "hecho_judicial", publicado["id"], org["id"], ids_semilla["rol_acusado_id"])

    await search_engine.sincronizar_hecho_judicial(con, publicado["id"])
    await search_engine.sincronizar_hecho_judicial(con, pendiente["id"])

    async def _ya_aparece_publicado() -> bool:
        r = await search_engine.buscar(
            search_engine.INDICE_HECHOS_JUDICIALES, texto="E2E Meilisearch",
            filtro='estado_publicacion = "publicado"', facetas=[], limite=50, offset=0,
        )
        return str(publicado["id"]) in {hit["id"] for hit in r["hits"]}

    await _esperar_hasta(_ya_aparece_publicado)

    try:
        # Buscar por texto
        resultado = await search_engine.buscar(
            search_engine.INDICE_HECHOS_JUDICIALES,
            texto="E2E Meilisearch",
            filtro='estado_publicacion = "publicado"',
            facetas=["categoria_delito_id", "provincia_id"],
            limite=50,
            offset=0,
        )
        ids_encontrados = {hit["id"] for hit in resultado["hits"]}
        assert str(publicado["id"]) in ids_encontrados
        assert str(pendiente["id"]) not in ids_encontrados, "un hecho pendiente_aprobacion no debe aparecer nunca"

        # Forma de facetDistribution que queries.py espera
        assert "categoria_delito_id" in resultado["facetDistribution"]
        assert "provincia_id" in resultado["facetDistribution"]

        # Filtro por array organizaciones_ids ("el array contiene este valor")
        resultado_org = await search_engine.buscar(
            search_engine.INDICE_HECHOS_JUDICIALES,
            texto=None,
            filtro=f'estado_publicacion = "publicado" AND (organizaciones_ids = {org["id"]})',
            facetas=[],
            limite=50,
            offset=0,
        )
        assert str(publicado["id"]) in {hit["id"] for hit in resultado_org["hits"]}

        # Borrar: sincronizar un hecho que pasó a rechazado debe sacarlo del índice
        await con.execute("UPDATE hecho_judicial SET estado_publicacion = 'rechazado' WHERE id = $1", publicado["id"])
        await search_engine.sincronizar_hecho_judicial(con, publicado["id"])

        async def _ya_no_aparece() -> bool:
            r = await search_engine.buscar(
                search_engine.INDICE_HECHOS_JUDICIALES, texto="E2E Meilisearch",
                filtro='estado_publicacion = "publicado"', facetas=[], limite=50, offset=0,
            )
            return str(publicado["id"]) not in {hit["id"] for hit in r["hits"]}

        await _esperar_hasta(_ya_no_aparece)
    finally:
        # Limpieza: `con` revierte Postgres solo, pero Meilisearch no vive
        # dentro de esa transacción — hay que sacar los documentos a mano
        # para no ensuciar el índice de una corrida a la siguiente.
        await search_engine._eliminar(search_engine.INDICE_HECHOS_JUDICIALES, str(publicado["id"]))
        await search_engine._eliminar(search_engine.INDICE_HECHOS_JUDICIALES, str(pendiente["id"]))
