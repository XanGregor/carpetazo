"""
Tests de search_engine.py y de los constructores de filtro de Meilisearch
en queries.py. Estos NO requieren una instancia de Meilisearch corriendo
— son las partes puras (construcción de documentos y de filtros), que es
donde vive la lógica que se puede romper por un refactor sin que nadie
note nada hasta producción.

La sincronización end-to-end contra un Meilisearch real (indexar, buscar,
facetDistribution, exclusión de no-publicados, borrado) se validó a mano
contra una instancia real levantada en el entorno de desarrollo — no está
en esta suite porque encadenar dos servicios (Postgres + Meilisearch) en
cada corrida de tests es una carga de infraestructura que no vale la pena
para este proyecto todavía. Si más adelante se arma un docker-compose
para CI, ahí es donde agregar esa prueba.
"""
import datetime
import os

import pytest

from graphql_api import search_engine
from graphql_api.enums import EstadoJudicial, NivelFuente, TipoDeclaracion
from graphql_api.inputs import FiltroDeclaracion, FiltroHechoJudicial
from graphql_api.queries import _filtro_meilisearch_decl, _filtro_meilisearch_hj


# ---------------------------------------------------------------------------
# fecha_a_timestamp / habilitado
# ---------------------------------------------------------------------------

def test_fecha_a_timestamp_convierte_a_epoch():
    d = datetime.date(2022, 3, 1)
    ts = search_engine.fecha_a_timestamp(d)
    assert ts == int(datetime.datetime(2022, 3, 1).timestamp())


def test_fecha_a_timestamp_none_es_none():
    assert search_engine.fecha_a_timestamp(None) is None


def test_habilitado_responde_a_env_var_sin_reimportar(monkeypatch):
    monkeypatch.delenv("MEILISEARCH_URL", raising=False)
    assert search_engine.habilitado() is False
    monkeypatch.setenv("MEILISEARCH_URL", "http://localhost:7700")
    assert search_engine.habilitado() is True


# ---------------------------------------------------------------------------
# Documentos: fila de Postgres -> forma que indexa Meilisearch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_documento_hecho_judicial_incluye_nombres_de_relaciones(con, usuarios, ids_semilla):
    from graphql_api import db

    fila = await db.insertar_hecho_judicial(
        con, titulo="Causa de prueba", descripcion="Descripción de prueba",
        categoria_delito_id=ids_semilla["categoria_delito_id"], estado_judicial=EstadoJudicial.PROCESADO.value,
        fecha_hecho=datetime.date(2022, 3, 1), provincia_id=ids_semilla["provincia_id"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )
    persona = await con.fetchrow(
        "INSERT INTO persona (nombre_completo) VALUES ('Persona de Prueba') RETURNING id"
    )
    await db.insertar_vinculo_persona(con, "hecho_judicial", fila["id"], persona["id"], ids_semilla["rol_acusado_id"])
    org = await con.fetchrow("INSERT INTO organizacion (nombre, tipo) VALUES ('Org de Prueba', 'partido_politico') RETURNING id")
    await db.insertar_vinculo_organizacion(con, "hecho_judicial", fila["id"], org["id"], ids_semilla["rol_acusado_id"])

    vista = await db.obtener_vista_busqueda_hecho_judicial(con, fila["id"])
    documento = search_engine._documento_hecho_judicial(vista)

    assert documento["id"] == str(fila["id"])
    assert documento["codigo"] == fila["codigo"]
    assert documento["titulo"] == "Causa de prueba"
    assert documento["categoria_delito_id"] == ids_semilla["categoria_delito_id"]
    assert documento["estado_judicial"] == "procesado"
    assert documento["estado_publicacion"] == "publicado"
    assert documento["organizaciones_ids"] == [org["id"]]
    assert documento["organizaciones_nombres"] == ["Org de Prueba"]
    assert documento["personas_nombres"] == ["Persona de Prueba"]
    assert documento["fecha_hecho"] == search_engine.fecha_a_timestamp(datetime.date(2022, 3, 1))


@pytest.mark.asyncio
async def test_documento_hecho_judicial_sin_relaciones_trae_listas_vacias(con, usuarios, ids_semilla):
    """COALESCE en la vista SQL evita que un LEFT JOIN LATERAL sin filas dé NULL —
    si esto se rompe, el documento indexado tendría None donde Meilisearch espera un array."""
    from graphql_api import db

    fila = await db.insertar_hecho_judicial(
        con, titulo="Causa sin vínculos", descripcion="...",
        categoria_delito_id=ids_semilla["categoria_delito_id"], estado_judicial=EstadoJudicial.DENUNCIA.value,
        fecha_hecho=None, provincia_id=None, estado_publicacion="borrador",
        creado_por=usuarios["admin"].id, aprobado_por=None,
    )
    vista = await db.obtener_vista_busqueda_hecho_judicial(con, fila["id"])
    documento = search_engine._documento_hecho_judicial(vista)

    assert documento["organizaciones_ids"] == []
    assert documento["organizaciones_nombres"] == []
    assert documento["personas_nombres"] == []
    assert documento["fecha_hecho"] is None
    assert documento["estado_publicacion"] == "borrador"


@pytest.mark.asyncio
async def test_documento_declaracion_basico(con, usuarios, ids_semilla):
    from graphql_api import db

    fila = await db.insertar_declaracion(
        con, titulo="Declaración de prueba", descripcion="...", tipo=TipoDeclaracion.VOTO_LEGISLATIVO.value,
        fecha=datetime.date(2021, 5, 1), provincia_id=ids_semilla["provincia_id"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )
    vista = await db.obtener_vista_busqueda_declaracion(con, fila["id"])
    documento = search_engine._documento_declaracion(vista)

    assert documento["id"] == str(fila["id"])
    assert documento["tipo"] == "voto_legislativo"
    assert documento["estado_publicacion"] == "publicado"
    assert documento["organizaciones_ids"] == []


# ---------------------------------------------------------------------------
# Constructores de filtro Meilisearch (OR interno, AND entre facetas)
# ---------------------------------------------------------------------------

def test_filtro_hj_vacio_solo_exige_publicado():
    assert _filtro_meilisearch_hj(FiltroHechoJudicial()) == 'estado_publicacion = "publicado"'


def test_filtro_hj_or_interno_entre_categorias():
    filtro = FiltroHechoJudicial(categorias_delito_ids=["3", "7"])
    resultado = _filtro_meilisearch_hj(filtro)
    assert 'estado_publicacion = "publicado"' in resultado
    assert "(categoria_delito_id = 3 OR categoria_delito_id = 7)" in resultado


def test_filtro_hj_and_entre_categorias_y_provincia():
    filtro = FiltroHechoJudicial(categorias_delito_ids=["3"], provincias_ids=["12"])
    resultado = _filtro_meilisearch_hj(filtro)
    partes = resultado.split(" AND ")
    assert '(categoria_delito_id = 3)' in partes
    assert '(provincia_id = 12)' in partes


def test_filtro_hj_estados_judiciales_usa_comillas():
    filtro = FiltroHechoJudicial(estados_judiciales=[EstadoJudicial.CONDENADO])
    resultado = _filtro_meilisearch_hj(filtro)
    assert '(estado_judicial = "condenado")' in resultado


def test_filtro_hj_rango_de_fechas():
    filtro = FiltroHechoJudicial(fecha_desde=datetime.date(2020, 1, 1), fecha_hasta=datetime.date(2023, 1, 1))
    resultado = _filtro_meilisearch_hj(filtro)
    assert f"fecha_hecho >= {search_engine.fecha_a_timestamp(datetime.date(2020, 1, 1))}" in resultado
    assert f"fecha_hecho <= {search_engine.fecha_a_timestamp(datetime.date(2023, 1, 1))}" in resultado


def test_filtro_decl_vacio_solo_exige_publicado():
    assert _filtro_meilisearch_decl(FiltroDeclaracion()) == 'estado_publicacion = "publicado"'


def test_filtro_decl_or_interno_entre_tipos():
    filtro = FiltroDeclaracion(tipos=[TipoDeclaracion.VOTO_LEGISLATIVO, TipoDeclaracion.PROYECTO_LEY])
    resultado = _filtro_meilisearch_decl(filtro)
    assert '(tipo = "voto_legislativo" OR tipo = "proyecto_ley")' in resultado


def test_filtro_hj_organizaciones_ids_es_igualdad_simple():
    """Meilisearch trata 'campo_array = valor' como 'el array contiene valor' —
    acá solo confirmamos que armamos ese formato, la semántica se validó a mano
    contra una instancia real (ver README)."""
    filtro = FiltroHechoJudicial(organizaciones_ids=["42"])
    resultado = _filtro_meilisearch_hj(filtro)
    assert "(organizaciones_ids = 42)" in resultado
