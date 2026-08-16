"""
Capa de acceso a datos. Todas las funciones acá reciben una conexión/pool
de asyncpg y devuelven asyncpg.Record (o listas de ellos) — la conversión
a tipos de Strawberry pasa en queries.py / mutations.py / dataloaders.py,
no acá, para mantener esta capa reutilizable fuera de GraphQL si hace falta
(ej: un endpoint REST de exportación a futuro).

Las funciones de "search_*" arman el WHERE dinámicamente: cada filtro de
lista usa `= ANY($n)` (OR interno) y los distintos filtros se combinan
con AND — la lógica que se definió para el buscador tipo Letterboxd.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date
from typing import Any, Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None
_pool_lock: Optional[asyncio.Lock] = None


async def get_pool() -> asyncpg.Pool:
    """
    min_size/max_size configurables por variable de entorno
    (DB_POOL_MIN_SIZE / DB_POOL_MAX_SIZE), con los mismos valores que ya
    había hardcodeados como default — así se puede calibrar el tamaño del
    pool en producción sin redeploy, y el script de carga
    (scripts/test_carga_pool.py) puede recrear el pool con otro tamaño
    dentro del mismo proceso para comparar configuraciones (cerrar con
    close_pool() y volver a llamar get_pool() con la env var cambiada).

    El chequeo `if _pool is None` está protegido con un lock (double-checked
    locking) — SIN el lock, si varios requests concurrentes le pegan a un
    pool recién frío (el caso típico: una ráfaga de tráfico justo después
    de levantar la app), todos ven `_pool is None` a la vez y cada uno
    dispara su propio `asyncpg.create_pool(...)` antes de que el primero
    termine de asignarse a la variable global — los pools "perdedores" de
    esa carrera quedan huérfanos: nada vuelve a referenciarlos, así que
    nunca se cierran, y sus conexiones quedan abiertas en Postgres hasta
    que se cae la app. Esto no era hipotético: se detectó de verdad
    escribiendo el test de carga/concurrencia (scripts/test_carga_pool.py)
    — un burst de 20 requests contra un pool frío terminaba en
    `TooManyConnectionsError: sorry, too many clients already`, y
    aislando el problema se confirmó que 20 llamadas concurrentes a
    get_pool() sin este lock creaban 20 pools distintos en vez de 1.

    El lock en sí se crea perezosamente (no a nivel de módulo) y se
    resetea junto con el pool en close_pool(): un asyncio.Lock() creado a
    nivel de módulo se ata al primer event loop que lo usa (en su primer
    `async with`), lo cual revienta con "bound to a different event loop"
    en cuanto otro código (típicamente un test distinto, cada uno con su
    propio event loop) intenta usar ese mismo lock — también se detectó
    de verdad al escribir el test de regresión (tests/test_db_pool.py).
    Crear el Lock() en sí es seguro sin protección extra porque no hay
    ningún `await` de por medio: dentro del modelo cooperativo de asyncio
    ese chequeo corre atómico, no hay forma de que dos tasks se
    entrelacen justo ahí.
    """
    global _pool, _pool_lock
    if _pool is None:
        if _pool_lock is None:
            _pool_lock = asyncio.Lock()
        async with _pool_lock:
            if _pool is None:  # otro task pudo haber creado el pool mientras esperábamos el lock
                _pool = await asyncpg.create_pool(
                    dsn=os.environ["DATABASE_URL"],
                    min_size=int(os.environ.get("DB_POOL_MIN_SIZE", "2")),
                    max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "10")),
                )
    return _pool


async def close_pool() -> None:
    global _pool, _pool_lock
    if _pool is not None:
        await _pool.close()
        _pool = None
    _pool_lock = None


# ---------------------------------------------------------------------------
# Lectura por id / código — usadas por DataLoaders y por queries de ficha
# ---------------------------------------------------------------------------

async def fetch_por_ids(con: asyncpg.Connection, tabla: str, ids: list[int]) -> list[asyncpg.Record]:
    """Uso interno de los DataLoaders: trae varias filas de una tabla simple por id, en el orden que sea."""
    return await con.fetch(f"SELECT * FROM {tabla} WHERE id = ANY($1::bigint[])", ids)


async def fetch_persona_por_codigo(con: asyncpg.Connection, codigo: str) -> Optional[asyncpg.Record]:
    return await con.fetchrow("SELECT * FROM persona WHERE codigo = $1", codigo)


async def fetch_organizacion_por_codigo(con: asyncpg.Connection, codigo: str) -> Optional[asyncpg.Record]:
    return await con.fetchrow("SELECT * FROM organizacion WHERE codigo = $1", codigo)


async def fetch_hecho_judicial_por_codigo(con: asyncpg.Connection, codigo: str) -> Optional[asyncpg.Record]:
    return await con.fetchrow(
        "SELECT * FROM hecho_judicial WHERE codigo = $1 AND estado_publicacion = 'publicado'", codigo
    )


async def fetch_declaracion_por_codigo(con: asyncpg.Connection, codigo: str) -> Optional[asyncpg.Record]:
    return await con.fetchrow(
        "SELECT * FROM declaracion WHERE codigo = $1 AND estado_publicacion = 'publicado'", codigo
    )


# ---------------------------------------------------------------------------
# Búsqueda faceteada de hechos judiciales
# ---------------------------------------------------------------------------

async def search_hechos_judiciales(
    con: asyncpg.Connection,
    *,
    categorias_delito_ids: Optional[list[int]],
    organizaciones_ids: Optional[list[int]],
    estados_judiciales: Optional[list[str]],
    provincias_ids: Optional[list[int]],
    fecha_desde: Optional[date],
    fecha_hasta: Optional[date],
    texto: Optional[str],
    limite: int,
    cursor_id: Optional[int],
) -> list[asyncpg.Record]:
    condiciones = ["hj.estado_publicacion = 'publicado'"]
    params: list[Any] = []

    def agregar(cond: str, valor: Any) -> None:
        params.append(valor)
        condiciones.append(cond.format(n=len(params)))

    if categorias_delito_ids:
        agregar("hj.categoria_delito_id = ANY(${n}::int[])", categorias_delito_ids)
    if estados_judiciales:
        agregar("hj.estado_judicial = ANY(${n}::estado_judicial[])", estados_judiciales)
    if provincias_ids:
        agregar("hj.provincia_id = ANY(${n}::int[])", provincias_ids)
    if fecha_desde:
        agregar("hj.fecha_hecho >= ${n}", fecha_desde)
    if fecha_hasta:
        agregar("hj.fecha_hecho <= ${n}", fecha_hasta)
    if texto:
        agregar("(hj.titulo ILIKE ${n} OR hj.descripcion ILIKE ${n})", f"%{texto}%")
        # nota: en producción esta condición de texto se reemplaza por una
        # consulta a Meilisearch/Typesense (ver README) — ILIKE acá es solo
        # el camino de fallback / desarrollo local sin motor de búsqueda.
    if organizaciones_ids:
        agregar(
            "EXISTS (SELECT 1 FROM hecho_organizacion ho WHERE ho.hecho_tipo = 'hecho_judicial' "
            "AND ho.hecho_id = hj.id AND ho.organizacion_id = ANY(${n}::bigint[]))",
            organizaciones_ids,
        )
    if cursor_id:
        agregar("hj.id < ${n}", cursor_id)

    params.append(limite)
    sql = f"""
        SELECT hj.* FROM hecho_judicial hj
        WHERE {' AND '.join(condiciones)}
        ORDER BY hj.id DESC
        LIMIT ${len(params)}
    """
    return await con.fetch(sql, *params)


async def contar_facetas_hecho_judicial(
    con: asyncpg.Connection,
    *,
    categorias_delito_ids: Optional[list[int]],
    estados_judiciales: Optional[list[str]],
    organizaciones_ids: Optional[list[int]],
    provincias_ids: Optional[list[int]],
    fecha_desde: Optional[date],
    fecha_hasta: Optional[date],
    texto: Optional[str],
) -> dict[str, list[dict]]:
    """
    Conteos por categoría de delito / estado judicial / provincia, aplicando
    los DEMÁS filtros activos (no el de la propia faceta que se está
    contando) — así el usuario ve "Lavado (23)" ya filtrado por lo que
    seleccionó en las otras columnas, igual que en Letterboxd.

    Cada faceta arma su PROPIO WHERE (_construir_where con excluir=...) en
    vez de compartir uno solo entre las tres — antes las tres queries
    reusaban el mismo WHERE, que incluía el filtro de provincia incluso
    para calcular la propia faceta de provincias (contradecía el docstring:
    si el usuario ya filtró por Mendoza, el desplegable de provincias
    quedaba mostrando SOLO Mendoza en vez de todas las provincias con sus
    conteos como alternativa). categorias_delito_ids/estados_judiciales
    tampoco se aceptaban como parámetro, así que elegir una categoría o un
    estado no tenía ningún efecto sobre ningún conteo de faceta.

    Devuelve dicts planos (no asyncpg.Record) para que el resultado sea
    directamente cacheable/serializable a JSON — ver queries.py, que
    envuelve esta llamada con facet_cache.obtener_o_calcular.
    """

    def _construir_where(*, excluir: str) -> tuple[str, list[Any]]:
        condiciones = ["hj.estado_publicacion = 'publicado'"]
        params: list[Any] = []

        def agregar(cond: str, valor: Any) -> None:
            params.append(valor)
            condiciones.append(cond.format(n=len(params)))

        if categorias_delito_ids and excluir != "categoria":
            agregar("hj.categoria_delito_id = ANY(${n}::int[])", categorias_delito_ids)
        if estados_judiciales and excluir != "estado":
            agregar("hj.estado_judicial = ANY(${n}::estado_judicial[])", estados_judiciales)
        if provincias_ids and excluir != "provincia":
            agregar("hj.provincia_id = ANY(${n}::int[])", provincias_ids)
        if fecha_desde:
            agregar("hj.fecha_hecho >= ${n}", fecha_desde)
        if fecha_hasta:
            agregar("hj.fecha_hecho <= ${n}", fecha_hasta)
        if texto:
            agregar("(hj.titulo ILIKE ${n} OR hj.descripcion ILIKE ${n})", f"%{texto}%")
        if organizaciones_ids:
            agregar(
                "EXISTS (SELECT 1 FROM hecho_organizacion ho WHERE ho.hecho_tipo = 'hecho_judicial' "
                "AND ho.hecho_id = hj.id AND ho.organizacion_id = ANY(${n}::bigint[]))",
                organizaciones_ids,
            )
        return " AND ".join(condiciones), params

    where_categoria, params_categoria = _construir_where(excluir="categoria")
    categorias = await con.fetch(
        f"""
        SELECT cd.id, cd.nombre, COUNT(*) AS cantidad
        FROM hecho_judicial hj JOIN categoria_delito cd ON cd.id = hj.categoria_delito_id
        WHERE {where_categoria}
        GROUP BY cd.id, cd.nombre ORDER BY cantidad DESC
        """,
        *params_categoria,
    )

    where_estado, params_estado = _construir_where(excluir="estado")
    estados = await con.fetch(
        f"""
        SELECT hj.estado_judicial AS valor, COUNT(*) AS cantidad
        FROM hecho_judicial hj WHERE {where_estado}
        GROUP BY hj.estado_judicial ORDER BY cantidad DESC
        """,
        *params_estado,
    )

    where_provincia, params_provincia = _construir_where(excluir="provincia")
    provincias = await con.fetch(
        f"""
        SELECT p.id, p.nombre, COUNT(*) AS cantidad
        FROM hecho_judicial hj JOIN provincia p ON p.id = hj.provincia_id
        WHERE {where_provincia}
        GROUP BY p.id, p.nombre ORDER BY cantidad DESC
        """,
        *params_provincia,
    )

    # Total con TODOS los filtros aplicados (sin excluir ninguno) — no
    # alcanza con sumar la faceta de categorías: esa, a propósito, excluye
    # el filtro de categoría, así que sumarla da "resultados en cualquier
    # categoría" en vez de "resultados que matchean todo lo que el usuario
    # filtró" en cuanto hay un filtro de categoría activo (ver el
    # docstring de arriba — este era otro síntoma del mismo problema).
    where_total, params_total = _construir_where(excluir="ninguno")
    total = await con.fetchval(f"SELECT COUNT(*) FROM hecho_judicial hj WHERE {where_total}", *params_total)

    return {
        "categorias_delito": [dict(r) for r in categorias],
        "estados_judiciales": [dict(r) for r in estados],
        "provincias": [dict(r) for r in provincias],
        "total": total,
    }


# ---------------------------------------------------------------------------
# Búsqueda faceteada de declaraciones (misma lógica, tabla distinta)
# ---------------------------------------------------------------------------

async def search_declaraciones(
    con: asyncpg.Connection,
    *,
    tipos: Optional[list[str]],
    organizaciones_ids: Optional[list[int]],
    provincias_ids: Optional[list[int]],
    fecha_desde: Optional[date],
    fecha_hasta: Optional[date],
    texto: Optional[str],
    limite: int,
    cursor_id: Optional[int],
) -> list[asyncpg.Record]:
    condiciones = ["d.estado_publicacion = 'publicado'"]
    params: list[Any] = []

    def agregar(cond: str, valor: Any) -> None:
        params.append(valor)
        condiciones.append(cond.format(n=len(params)))

    if tipos:
        agregar("d.tipo = ANY(${n}::tipo_declaracion[])", tipos)
    if provincias_ids:
        agregar("d.provincia_id = ANY(${n}::int[])", provincias_ids)
    if fecha_desde:
        agregar("d.fecha >= ${n}", fecha_desde)
    if fecha_hasta:
        agregar("d.fecha <= ${n}", fecha_hasta)
    if texto:
        agregar("(d.titulo ILIKE ${n} OR d.descripcion ILIKE ${n})", f"%{texto}%")
    if organizaciones_ids:
        agregar(
            "EXISTS (SELECT 1 FROM hecho_organizacion ho WHERE ho.hecho_tipo = 'declaracion' "
            "AND ho.hecho_id = d.id AND ho.organizacion_id = ANY(${n}::bigint[]))",
            organizaciones_ids,
        )
    if cursor_id:
        agregar("d.id < ${n}", cursor_id)

    params.append(limite)
    sql = f"""
        SELECT d.* FROM declaracion d
        WHERE {' AND '.join(condiciones)}
        ORDER BY d.id DESC
        LIMIT ${len(params)}
    """
    return await con.fetch(sql, *params)


async def contar_facetas_declaracion(
    con: asyncpg.Connection,
    *,
    tipos: Optional[list[str]],
    organizaciones_ids: Optional[list[int]],
    provincias_ids: Optional[list[int]],
    fecha_desde: Optional[date],
    fecha_hasta: Optional[date],
    texto: Optional[str],
) -> dict[str, list[dict]]:
    """Mismo arreglo que contar_facetas_hecho_judicial: WHERE separado por
    faceta (excluyendo el filtro propio de cada una) y devuelve dicts
    planos en vez de asyncpg.Record — ver el docstring de esa función para
    el detalle de qué estaba mal antes."""

    def _construir_where(*, excluir: str) -> tuple[str, list[Any]]:
        condiciones = ["d.estado_publicacion = 'publicado'"]
        params: list[Any] = []

        def agregar(cond: str, valor: Any) -> None:
            params.append(valor)
            condiciones.append(cond.format(n=len(params)))

        if tipos and excluir != "tipo":
            agregar("d.tipo = ANY(${n}::tipo_declaracion[])", tipos)
        if provincias_ids and excluir != "provincia":
            agregar("d.provincia_id = ANY(${n}::int[])", provincias_ids)
        if fecha_desde:
            agregar("d.fecha >= ${n}", fecha_desde)
        if fecha_hasta:
            agregar("d.fecha <= ${n}", fecha_hasta)
        if texto:
            agregar("(d.titulo ILIKE ${n} OR d.descripcion ILIKE ${n})", f"%{texto}%")
        if organizaciones_ids:
            agregar(
                "EXISTS (SELECT 1 FROM hecho_organizacion ho WHERE ho.hecho_tipo = 'declaracion' "
                "AND ho.hecho_id = d.id AND ho.organizacion_id = ANY(${n}::bigint[]))",
                organizaciones_ids,
            )
        return " AND ".join(condiciones), params

    where_tipo, params_tipo = _construir_where(excluir="tipo")
    tipos_conteo = await con.fetch(
        f"SELECT d.tipo AS valor, COUNT(*) AS cantidad FROM declaracion d WHERE {where_tipo} GROUP BY d.tipo ORDER BY cantidad DESC",
        *params_tipo,
    )

    where_provincia, params_provincia = _construir_where(excluir="provincia")
    provincias = await con.fetch(
        f"""
        SELECT p.id, p.nombre, COUNT(*) AS cantidad
        FROM declaracion d JOIN provincia p ON p.id = d.provincia_id
        WHERE {where_provincia}
        GROUP BY p.id, p.nombre ORDER BY cantidad DESC
        """,
        *params_provincia,
    )

    where_total, params_total = _construir_where(excluir="ninguno")
    total = await con.fetchval(f"SELECT COUNT(*) FROM declaracion d WHERE {where_total}", *params_total)

    return {
        "tipos": [dict(r) for r in tipos_conteo],
        "provincias": [dict(r) for r in provincias],
        "total": total,
    }


# ---------------------------------------------------------------------------
# Vistas denormalizadas para el motor de búsqueda (ver search_engine.py)
# ---------------------------------------------------------------------------

async def obtener_vista_busqueda_hecho_judicial(con: asyncpg.Connection, hecho_id: int) -> Optional[asyncpg.Record]:
    return await con.fetchrow(
        """
        SELECT
            hj.id, hj.codigo, hj.titulo, hj.descripcion, hj.categoria_delito_id,
            hj.estado_judicial, hj.fecha_hecho, hj.provincia_id, hj.estado_publicacion,
            COALESCE(orgs.ids, ARRAY[]::bigint[]) AS organizaciones_ids,
            COALESCE(orgs.nombres, ARRAY[]::text[]) AS organizaciones_nombres,
            COALESCE(pers.nombres, ARRAY[]::text[]) AS personas_nombres
        FROM hecho_judicial hj
        LEFT JOIN LATERAL (
            SELECT array_agg(o.id) AS ids, array_agg(o.nombre) AS nombres
            FROM hecho_organizacion ho JOIN organizacion o ON o.id = ho.organizacion_id
            WHERE ho.hecho_tipo = 'hecho_judicial' AND ho.hecho_id = hj.id
        ) orgs ON true
        LEFT JOIN LATERAL (
            SELECT array_agg(p.nombre_completo) AS nombres
            FROM hecho_persona hp JOIN persona p ON p.id = hp.persona_id
            WHERE hp.hecho_tipo = 'hecho_judicial' AND hp.hecho_id = hj.id
        ) pers ON true
        WHERE hj.id = $1
        """,
        hecho_id,
    )


async def obtener_vista_busqueda_declaracion(con: asyncpg.Connection, declaracion_id: int) -> Optional[asyncpg.Record]:
    return await con.fetchrow(
        """
        SELECT
            d.id, d.codigo, d.titulo, d.descripcion, d.tipo,
            d.fecha, d.provincia_id, d.estado_publicacion,
            COALESCE(orgs.ids, ARRAY[]::bigint[]) AS organizaciones_ids,
            COALESCE(orgs.nombres, ARRAY[]::text[]) AS organizaciones_nombres,
            COALESCE(pers.nombres, ARRAY[]::text[]) AS personas_nombres
        FROM declaracion d
        LEFT JOIN LATERAL (
            SELECT array_agg(o.id) AS ids, array_agg(o.nombre) AS nombres
            FROM hecho_organizacion ho JOIN organizacion o ON o.id = ho.organizacion_id
            WHERE ho.hecho_tipo = 'declaracion' AND ho.hecho_id = d.id
        ) orgs ON true
        LEFT JOIN LATERAL (
            SELECT array_agg(p.nombre_completo) AS nombres
            FROM hecho_persona hp JOIN persona p ON p.id = hp.persona_id
            WHERE hp.hecho_tipo = 'declaracion' AND hp.hecho_id = d.id
        ) pers ON true
        WHERE d.id = $1
        """,
        declaracion_id,
    )


# ---------------------------------------------------------------------------
# Recorrido masivo de publicados — usado por scripts/reindexar_meilisearch.py
# para el reindexado inicial (primer deploy con Meilisearch, o reconstruir
# el índice desde cero). Mismas columnas que obtener_vista_busqueda_* de
# arriba (así search_engine._documento_* produce el mismo documento que la
# sincronización normal), pero trayendo muchas filas por vez en vez de una,
# paginado por cursor de id para no cargar todo el archivo en memoria de una.
# ---------------------------------------------------------------------------

async def contar_hechos_judiciales_publicados(con: asyncpg.Connection) -> int:
    return await con.fetchval("SELECT COUNT(*) FROM hecho_judicial WHERE estado_publicacion = 'publicado'")


async def contar_declaraciones_publicadas(con: asyncpg.Connection) -> int:
    return await con.fetchval("SELECT COUNT(*) FROM declaracion WHERE estado_publicacion = 'publicado'")


async def fetch_lote_vista_busqueda_hechos_judiciales(
    con: asyncpg.Connection, *, cursor_id: Optional[int], limite: int
) -> list[asyncpg.Record]:
    condiciones = ["hj.estado_publicacion = 'publicado'"]
    params: list[Any] = []

    def agregar(cond: str, valor: Any) -> None:
        params.append(valor)
        condiciones.append(cond.format(n=len(params)))

    if cursor_id is not None:
        agregar("hj.id > ${n}", cursor_id)

    params.append(limite)
    sql = f"""
        SELECT
            hj.id, hj.codigo, hj.titulo, hj.descripcion, hj.categoria_delito_id,
            hj.estado_judicial, hj.fecha_hecho, hj.provincia_id, hj.estado_publicacion,
            COALESCE(orgs.ids, ARRAY[]::bigint[]) AS organizaciones_ids,
            COALESCE(orgs.nombres, ARRAY[]::text[]) AS organizaciones_nombres,
            COALESCE(pers.nombres, ARRAY[]::text[]) AS personas_nombres
        FROM hecho_judicial hj
        LEFT JOIN LATERAL (
            SELECT array_agg(o.id) AS ids, array_agg(o.nombre) AS nombres
            FROM hecho_organizacion ho JOIN organizacion o ON o.id = ho.organizacion_id
            WHERE ho.hecho_tipo = 'hecho_judicial' AND ho.hecho_id = hj.id
        ) orgs ON true
        LEFT JOIN LATERAL (
            SELECT array_agg(p.nombre_completo) AS nombres
            FROM hecho_persona hp JOIN persona p ON p.id = hp.persona_id
            WHERE hp.hecho_tipo = 'hecho_judicial' AND hp.hecho_id = hj.id
        ) pers ON true
        WHERE {' AND '.join(condiciones)}
        ORDER BY hj.id ASC
        LIMIT ${len(params)}
    """
    return await con.fetch(sql, *params)


async def fetch_lote_vista_busqueda_declaraciones(
    con: asyncpg.Connection, *, cursor_id: Optional[int], limite: int
) -> list[asyncpg.Record]:
    condiciones = ["d.estado_publicacion = 'publicado'"]
    params: list[Any] = []

    def agregar(cond: str, valor: Any) -> None:
        params.append(valor)
        condiciones.append(cond.format(n=len(params)))

    if cursor_id is not None:
        agregar("d.id > ${n}", cursor_id)

    params.append(limite)
    sql = f"""
        SELECT
            d.id, d.codigo, d.titulo, d.descripcion, d.tipo,
            d.fecha, d.provincia_id, d.estado_publicacion,
            COALESCE(orgs.ids, ARRAY[]::bigint[]) AS organizaciones_ids,
            COALESCE(orgs.nombres, ARRAY[]::text[]) AS organizaciones_nombres,
            COALESCE(pers.nombres, ARRAY[]::text[]) AS personas_nombres
        FROM declaracion d
        LEFT JOIN LATERAL (
            SELECT array_agg(o.id) AS ids, array_agg(o.nombre) AS nombres
            FROM hecho_organizacion ho JOIN organizacion o ON o.id = ho.organizacion_id
            WHERE ho.hecho_tipo = 'declaracion' AND ho.hecho_id = d.id
        ) orgs ON true
        LEFT JOIN LATERAL (
            SELECT array_agg(p.nombre_completo) AS nombres
            FROM hecho_persona hp JOIN persona p ON p.id = hp.persona_id
            WHERE hp.hecho_tipo = 'declaracion' AND hp.hecho_id = d.id
        ) pers ON true
        WHERE {' AND '.join(condiciones)}
        ORDER BY d.id ASC
        LIMIT ${len(params)}
    """
    return await con.fetch(sql, *params)


# ---------------------------------------------------------------------------
# Escritura: hechos, fuentes, vínculos, relaciones, reportes, api keys
# ---------------------------------------------------------------------------

async def insertar_hecho_judicial(
    con: asyncpg.Connection,
    *,
    titulo: str,
    descripcion: str,
    categoria_delito_id: int,
    estado_judicial: str,
    fecha_hecho: Optional[date],
    provincia_id: Optional[int],
    estado_publicacion: str,
    creado_por: int,
    aprobado_por: Optional[int],
) -> asyncpg.Record:
    return await con.fetchrow(
        """
        INSERT INTO hecho_judicial
            (titulo, descripcion, categoria_delito_id, estado_judicial, fecha_hecho,
             provincia_id, estado_publicacion, creado_por, aprobado_por)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING *
        """,
        titulo, descripcion, categoria_delito_id, estado_judicial, fecha_hecho,
        provincia_id, estado_publicacion, creado_por, aprobado_por,
    )


async def insertar_declaracion(
    con: asyncpg.Connection,
    *,
    titulo: str,
    descripcion: str,
    tipo: str,
    fecha: Optional[date],
    provincia_id: Optional[int],
    estado_publicacion: str,
    creado_por: int,
    aprobado_por: Optional[int],
) -> asyncpg.Record:
    return await con.fetchrow(
        """
        INSERT INTO declaracion
            (titulo, descripcion, tipo, fecha, provincia_id, estado_publicacion, creado_por, aprobado_por)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING *
        """,
        titulo, descripcion, tipo, fecha, provincia_id, estado_publicacion, creado_por, aprobado_por,
    )


async def actualizar_estado_publicacion(
    con: asyncpg.Connection, tabla: str, id_: int, estado: str, aprobado_por: Optional[int]
) -> asyncpg.Record:
    return await con.fetchrow(
        f"UPDATE {tabla} SET estado_publicacion = $2, aprobado_por = $3 WHERE id = $1 RETURNING *",
        id_, estado, aprobado_por,
    )


async def insertar_fuente(
    con: asyncpg.Connection,
    *,
    hecho_judicial_id: Optional[int] = None,
    declaracion_id: Optional[int] = None,
    financiamiento_id: Optional[int] = None,
    hecho_relacion_id: Optional[int] = None,
    nivel: str,
    tipo_documento: Optional[str],
    url: Optional[str],
    medio_institucion: Optional[str],
    fecha_publicacion: Optional[date],
) -> asyncpg.Record:
    return await con.fetchrow(
        """
        INSERT INTO fuente
            (hecho_judicial_id, declaracion_id, financiamiento_id, hecho_relacion_id,
             nivel, tipo_documento, url, medio_institucion, fecha_publicacion)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING *
        """,
        hecho_judicial_id, declaracion_id, financiamiento_id, hecho_relacion_id,
        nivel, tipo_documento, url, medio_institucion, fecha_publicacion,
    )


async def insertar_vinculo_persona(
    con: asyncpg.Connection, hecho_tipo: str, hecho_id: int, persona_id: int, rol_id: int
) -> None:
    await con.execute(
        "INSERT INTO hecho_persona (hecho_tipo, hecho_id, persona_id, rol_id) VALUES ($1, $2, $3, $4)",
        hecho_tipo, hecho_id, persona_id, rol_id,
    )


async def insertar_vinculo_organizacion(
    con: asyncpg.Connection, hecho_tipo: str, hecho_id: int, organizacion_id: int, rol_id: int
) -> None:
    await con.execute(
        "INSERT INTO hecho_organizacion (hecho_tipo, hecho_id, organizacion_id, rol_id) VALUES ($1, $2, $3, $4)",
        hecho_tipo, hecho_id, organizacion_id, rol_id,
    )


async def insertar_relacion_hecho(
    con: asyncpg.Connection,
    *,
    origen_tipo: str,
    origen_id: int,
    destino_tipo: str,
    destino_id: int,
    tipo_relacion: str,
    descripcion: Optional[str],
) -> asyncpg.Record:
    return await con.fetchrow(
        """
        INSERT INTO hecho_relacion (origen_tipo, origen_id, destino_tipo, destino_id, tipo_relacion, descripcion)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        origen_tipo, origen_id, destino_tipo, destino_id, tipo_relacion, descripcion,
    )


async def insertar_reporte(
    con: asyncpg.Connection, *, hecho_tipo: str, hecho_id: int, descripcion_problema: str, email_reportante: Optional[str]
) -> asyncpg.Record:
    return await con.fetchrow(
        """
        INSERT INTO reporte (hecho_tipo, hecho_id, descripcion_problema, email_reportante)
        VALUES ($1, $2, $3, $4)
        RETURNING *
        """,
        hecho_tipo, hecho_id, descripcion_problema, email_reportante,
    )


async def insertar_api_key(
    con: asyncpg.Connection, *, nombre: str, email: str, uso_previsto: Optional[str], key_hash: str
) -> asyncpg.Record:
    return await con.fetchrow(
        "INSERT INTO api_key (nombre, email, uso_previsto, key_hash) VALUES ($1, $2, $3, $4) RETURNING *",
        nombre, email, uso_previsto, key_hash,
    )


# -- Audit log tipo commit (ver 12.1/13 del schema SQL) ----------------------

async def insertar_commit(
    con: asyncpg.Connection, *, hash_: str, autor_id: int, descripcion: Optional[str], commit_padre_id: Optional[int] = None
) -> asyncpg.Record:
    return await con.fetchrow(
        "INSERT INTO commit (hash, autor_id, descripcion, commit_padre_id) VALUES ($1, $2, $3, $4) RETURNING *",
        hash_, autor_id, descripcion, commit_padre_id,
    )


async def insertar_cambio(
    con: asyncpg.Connection,
    *,
    commit_id: int,
    entidad_tipo: str,
    entidad_id: int,
    campo: str,
    valor_anterior: Optional[str],
    valor_nuevo: Optional[str],
) -> None:
    await con.execute(
        """
        INSERT INTO cambio (commit_id, entidad_tipo, entidad_id, campo, valor_anterior, valor_nuevo)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        commit_id, entidad_tipo, entidad_id, campo, valor_anterior, valor_nuevo,
    )


# ---------------------------------------------------------------------------
# Batch fetchers para DataLoaders (siempre reciben una lista de ids y
# devuelven los resultados agrupados por id de entrada, en el mismo orden)
# ---------------------------------------------------------------------------

async def batch_afiliaciones_por_persona(con: asyncpg.Connection, persona_ids: list[int]) -> dict[int, list[asyncpg.Record]]:
    filas = await con.fetch("SELECT * FROM afiliacion WHERE persona_id = ANY($1::bigint[])", persona_ids)
    agrupado: dict[int, list[asyncpg.Record]] = {pid: [] for pid in persona_ids}
    for fila in filas:
        agrupado[fila["persona_id"]].append(fila)
    return agrupado


async def batch_afiliaciones_por_organizacion(con: asyncpg.Connection, organizacion_ids: list[int]) -> dict[int, list[asyncpg.Record]]:
    filas = await con.fetch("SELECT * FROM afiliacion WHERE organizacion_id = ANY($1::bigint[])", organizacion_ids)
    agrupado: dict[int, list[asyncpg.Record]] = {oid: [] for oid in organizacion_ids}
    for fila in filas:
        agrupado[fila["organizacion_id"]].append(fila)
    return agrupado


async def batch_financiamiento_por_organizacion(con: asyncpg.Connection, organizacion_ids: list[int]) -> dict[int, list[asyncpg.Record]]:
    filas = await con.fetch(
        "SELECT * FROM organizacion_financiamiento WHERE organizacion_id = ANY($1::bigint[])", organizacion_ids
    )
    agrupado: dict[int, list[asyncpg.Record]] = {oid: [] for oid in organizacion_ids}
    for fila in filas:
        agrupado[fila["organizacion_id"]].append(fila)
    return agrupado


async def batch_fuentes(con: asyncpg.Connection, columna_fk: str, ids: list[int]) -> dict[int, list[asyncpg.Record]]:
    """columna_fk: 'hecho_judicial_id' | 'declaracion_id' | 'financiamiento_id' | 'hecho_relacion_id'."""
    filas = await con.fetch(f"SELECT * FROM fuente WHERE {columna_fk} = ANY($1::bigint[])", ids)
    agrupado: dict[int, list[asyncpg.Record]] = {i: [] for i in ids}
    for fila in filas:
        agrupado[fila[columna_fk]].append(fila)
    return agrupado


async def batch_vinculos_persona(
    con: asyncpg.Connection, hecho_tipo: str, hecho_ids: list[int]
) -> dict[int, list[asyncpg.Record]]:
    filas = await con.fetch(
        """
        SELECT hp.hecho_id, hp.rol_id, r.nombre AS rol_nombre, p.*
        FROM hecho_persona hp
        JOIN rol_en_hecho r ON r.id = hp.rol_id
        JOIN persona p ON p.id = hp.persona_id
        WHERE hp.hecho_tipo = $1 AND hp.hecho_id = ANY($2::bigint[])
        """,
        hecho_tipo,
        hecho_ids,
    )
    agrupado: dict[int, list[asyncpg.Record]] = {i: [] for i in hecho_ids}
    for fila in filas:
        agrupado[fila["hecho_id"]].append(fila)
    return agrupado


async def batch_vinculos_organizacion(
    con: asyncpg.Connection, hecho_tipo: str, hecho_ids: list[int]
) -> dict[int, list[asyncpg.Record]]:
    filas = await con.fetch(
        """
        SELECT ho.hecho_id, ho.rol_id, r.nombre AS rol_nombre, o.*
        FROM hecho_organizacion ho
        JOIN rol_en_hecho r ON r.id = ho.rol_id
        JOIN organizacion o ON o.id = ho.organizacion_id
        WHERE ho.hecho_tipo = $1 AND ho.hecho_id = ANY($2::bigint[])
        """,
        hecho_tipo,
        hecho_ids,
    )
    agrupado: dict[int, list[asyncpg.Record]] = {i: [] for i in hecho_ids}
    for fila in filas:
        agrupado[fila["hecho_id"]].append(fila)
    return agrupado


async def batch_hechos_por_persona(
    con: asyncpg.Connection, hecho_tipo: str, persona_ids: list[int]
) -> dict[int, list[asyncpg.Record]]:
    tabla = "hecho_judicial" if hecho_tipo == "hecho_judicial" else "declaracion"
    filas = await con.fetch(
        f"""
        SELECT hp.persona_id, h.*
        FROM hecho_persona hp
        JOIN {tabla} h ON h.id = hp.hecho_id
        WHERE hp.hecho_tipo = $1 AND hp.persona_id = ANY($2::bigint[]) AND h.estado_publicacion = 'publicado'
        """,
        hecho_tipo,
        persona_ids,
    )
    agrupado: dict[int, list[asyncpg.Record]] = {i: [] for i in persona_ids}
    for fila in filas:
        agrupado[fila["persona_id"]].append(fila)
    return agrupado


async def batch_hechos_por_organizacion(
    con: asyncpg.Connection, hecho_tipo: str, organizacion_ids: list[int]
) -> dict[int, list[asyncpg.Record]]:
    tabla = "hecho_judicial" if hecho_tipo == "hecho_judicial" else "declaracion"
    filas = await con.fetch(
        f"""
        SELECT ho.organizacion_id, h.*
        FROM hecho_organizacion ho
        JOIN {tabla} h ON h.id = ho.hecho_id
        WHERE ho.hecho_tipo = $1 AND ho.organizacion_id = ANY($2::bigint[]) AND h.estado_publicacion = 'publicado'
        """,
        hecho_tipo,
        organizacion_ids,
    )
    agrupado: dict[int, list[asyncpg.Record]] = {i: [] for i in organizacion_ids}
    for fila in filas:
        agrupado[fila["organizacion_id"]].append(fila)
    return agrupado


async def batch_relaciones_por_hecho(
    con: asyncpg.Connection, claves: list[tuple[str, int]]
) -> dict[tuple[str, int], list[asyncpg.Record]]:
    """claves: lista de (hecho_tipo, hecho_id). Trae relaciones donde el hecho aparece como origen O destino."""
    tipos = [c[0] for c in claves]
    ids = [c[1] for c in claves]
    filas = await con.fetch(
        """
        SELECT * FROM hecho_relacion
        WHERE (origen_tipo::text, origen_id) IN (SELECT * FROM unnest($1::text[], $2::bigint[]))
           OR (destino_tipo::text, destino_id) IN (SELECT * FROM unnest($1::text[], $2::bigint[]))
        """,
        tipos,
        ids,
    )
    agrupado: dict[tuple[str, int], list[asyncpg.Record]] = {c: [] for c in claves}
    for fila in filas:
        origen = (fila["origen_tipo"], fila["origen_id"])
        destino = (fila["destino_tipo"], fila["destino_id"])
        if origen in agrupado:
            agrupado[origen].append(fila)
        if destino in agrupado and destino != origen:
            agrupado[destino].append(fila)
    return agrupado
