"""
Tests del fallback de conteo de facetas por Postgres
(db.contar_facetas_hecho_judicial / contar_facetas_declaracion, usadas
por queries.py cuando Meilisearch no está configurado).

Cubre el bug real encontrado al implementar el cache (ver README, sección
"Fallback de Postgres: bug de facetas encontrado al cachear"): antes, las
tres queries de conteo de hecho_judicial compartían un único WHERE que
incluía el filtro de provincia incluso al calcular la propia faceta de
provincias (contradecía el docstring de la función), y ni siquiera
aceptaban categorias_delito_ids/estados_judiciales como filtro — así que
elegir una categoría no tenía ningún efecto en ningún conteo de faceta.
También cubre que `total` salga de un COUNT(*) con todos los filtros
aplicados, no de sumar una faceta que a propósito excluye su propio
filtro (eso daba un total inflado en cuanto había un filtro de categoría
activo).
"""
import pytest

from graphql_api import db


@pytest.mark.asyncio
async def test_faceta_de_categoria_excluye_su_propio_filtro(con, usuarios, ids_semilla):
    """Filtrando por categoria_1, la faceta de categorías debe seguir
    mostrando TODAS las categorías con resultados (no solo la
    seleccionada) — así el usuario puede ver y cambiar a otra opción,
    igual que el selector de género de Letterboxd."""
    await db.insertar_hecho_judicial(
        con, titulo="Caso A", descripcion="...", categoria_delito_id=ids_semilla["categoria_delito_id"],
        estado_judicial="procesado", fecha_hecho=None, provincia_id=ids_semilla["provincia_id"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )
    await db.insertar_hecho_judicial(
        con, titulo="Caso B", descripcion="...", categoria_delito_id=ids_semilla["categoria_delito_id_2"],
        estado_judicial="procesado", fecha_hecho=None, provincia_id=ids_semilla["provincia_id"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )
    await db.insertar_hecho_judicial(
        con, titulo="Caso C", descripcion="...", categoria_delito_id=ids_semilla["categoria_delito_id"],
        estado_judicial="procesado", fecha_hecho=None, provincia_id=ids_semilla["provincia_id_2"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )

    conteos = await db.contar_facetas_hecho_judicial(
        con,
        categorias_delito_ids=[ids_semilla["categoria_delito_id"]],
        estados_judiciales=None, organizaciones_ids=None, provincias_ids=None,
        fecha_desde=None, fecha_hasta=None, texto=None,
    )

    categorias_con_datos = {c["id"]: c["cantidad"] for c in conteos["categorias_delito"]}
    assert ids_semilla["categoria_delito_id"] in categorias_con_datos, "la propia categoría filtrada debe seguir apareciendo"
    assert ids_semilla["categoria_delito_id_2"] in categorias_con_datos, "otras categorías NO deben desaparecer del selector"
    assert categorias_con_datos[ids_semilla["categoria_delito_id"]] == 2  # A, C
    assert categorias_con_datos[ids_semilla["categoria_delito_id_2"]] == 1  # B


@pytest.mark.asyncio
async def test_faceta_de_provincia_respeta_el_filtro_de_categoria_activo(con, usuarios, ids_semilla):
    """La faceta de provincias excluye SU PROPIO filtro, pero sigue
    aplicando el filtro de categoría (que sigue activo) — antes esto no
    pasaba porque categorias_delito_ids ni se aceptaba como parámetro."""
    await db.insertar_hecho_judicial(
        con, titulo="Caso A", descripcion="...", categoria_delito_id=ids_semilla["categoria_delito_id"],
        estado_judicial="procesado", fecha_hecho=None, provincia_id=ids_semilla["provincia_id"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )
    await db.insertar_hecho_judicial(
        con, titulo="Caso B (otra categoría, misma provincia)", descripcion="...",
        categoria_delito_id=ids_semilla["categoria_delito_id_2"],
        estado_judicial="procesado", fecha_hecho=None, provincia_id=ids_semilla["provincia_id"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )
    await db.insertar_hecho_judicial(
        con, titulo="Caso C (misma categoría que A, otra provincia)", descripcion="...",
        categoria_delito_id=ids_semilla["categoria_delito_id"],
        estado_judicial="procesado", fecha_hecho=None, provincia_id=ids_semilla["provincia_id_2"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )

    conteos = await db.contar_facetas_hecho_judicial(
        con,
        categorias_delito_ids=[ids_semilla["categoria_delito_id"]],
        estados_judiciales=None, organizaciones_ids=None, provincias_ids=None,
        fecha_desde=None, fecha_hasta=None, texto=None,
    )

    provincias_con_datos = {p["id"]: p["cantidad"] for p in conteos["provincias"]}
    # Con el filtro de categoría activo, la faceta de provincias solo debe
    # contar A y C (categoría filtrada) — B (otra categoría) no debe sumar
    # a la provincia de B, aunque esa provincia coincida con la de A.
    assert provincias_con_datos.get(ids_semilla["provincia_id"]) == 1  # solo A, no B
    assert provincias_con_datos.get(ids_semilla["provincia_id_2"]) == 1  # C


@pytest.mark.asyncio
async def test_total_no_es_la_suma_de_una_faceta_que_excluye_su_propio_filtro(con, usuarios, ids_semilla):
    """Regresión directa del bug: antes `total` salía de sumar la faceta
    de categorías, que a propósito excluye el filtro de categoría — con
    un filtro de categoría activo eso daba un total inflado (contaba
    resultados de categorías no seleccionadas)."""
    await db.insertar_hecho_judicial(
        con, titulo="Caso A", descripcion="...", categoria_delito_id=ids_semilla["categoria_delito_id"],
        estado_judicial="procesado", fecha_hecho=None, provincia_id=ids_semilla["provincia_id"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )
    await db.insertar_hecho_judicial(
        con, titulo="Caso B (otra categoría)", descripcion="...", categoria_delito_id=ids_semilla["categoria_delito_id_2"],
        estado_judicial="procesado", fecha_hecho=None, provincia_id=ids_semilla["provincia_id"],
        estado_publicacion="publicado", creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )

    conteos = await db.contar_facetas_hecho_judicial(
        con,
        categorias_delito_ids=[ids_semilla["categoria_delito_id"]],
        estados_judiciales=None, organizaciones_ids=None, provincias_ids=None,
        fecha_desde=None, fecha_hasta=None, texto=None,
    )

    suma_de_la_faceta_categoria = sum(c["cantidad"] for c in conteos["categorias_delito"])
    assert suma_de_la_faceta_categoria == 2, "la faceta en sí (A+B) no cambia — confirma que sigue excluyendo su filtro"
    assert conteos["total"] == 1, "pero el total real, con el filtro de categoría aplicado, es solo A"
    assert conteos["total"] != suma_de_la_faceta_categoria


@pytest.mark.asyncio
async def test_declaracion_mismo_patron_de_faceta_y_total(con, usuarios, ids_semilla):
    await db.insertar_declaracion(
        con, titulo="Voto A", descripcion="...", tipo="voto_legislativo", fecha=None,
        provincia_id=ids_semilla["provincia_id"], estado_publicacion="publicado",
        creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )
    await db.insertar_declaracion(
        con, titulo="Proyecto B (otro tipo)", descripcion="...", tipo="proyecto_ley", fecha=None,
        provincia_id=ids_semilla["provincia_id"], estado_publicacion="publicado",
        creado_por=usuarios["admin"].id, aprobado_por=usuarios["admin"].id,
    )

    conteos = await db.contar_facetas_declaracion(
        con,
        tipos=["voto_legislativo"],
        organizaciones_ids=None, provincias_ids=None,
        fecha_desde=None, fecha_hasta=None, texto=None,
    )

    tipos_con_datos = {t["valor"]: t["cantidad"] for t in conteos["tipos"]}
    assert "voto_legislativo" in tipos_con_datos
    assert "proyecto_ley" in tipos_con_datos, "la faceta de tipos no debe colapsar al tipo filtrado"
    assert conteos["total"] == 1, "el total sí debe reflejar el filtro de tipo aplicado"
