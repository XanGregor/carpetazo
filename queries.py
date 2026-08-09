"""
Query root. Se usa tal cual en ambos schemas (interno y público) — leer
información ya publicada no requiere rol ni API key; lo que distingue a
la superficie pública es que su schema no expone Mutation en absoluto
(ver schema.py) y que su router exige X-API-Key antes de llegar acá
(ver context.py).

Cada resolver saca su propia conexión de info.context["pool"] con
`async with pool.acquire() as con:` — no se reutiliza una conexión
compartida entre resolvers, porque GraphQL puede resolver varios campos
en paralelo (ver la nota en dataloaders.py).

buscar_hechos_judiciales / buscar_declaraciones tienen DOS implementaciones:
  - _meilisearch: cuando search_engine.habilitado() — usa el motor para
    texto + filtros + facetDistribution (los conteos en vivo tipo
    Letterboxd salen nativos del motor, no hay que calcularlos a mano).
    Los IDs que devuelve se hidratan contra Postgres vía DataLoader, así
    que el motor decide QUÉ matchea pero Postgres sigue siendo la fuente
    de verdad de los datos que se muestran.
  - _postgres: fallback con ILIKE + GROUP BY, para desarrollo local sin
    tener Meilisearch corriendo, o si el motor está caído.

Nota sobre paginación: el cursor de Postgres es keyset (id < cursor) y el
de Meilisearch es offset numérico — son esquemas distintos por debajo del
mismo campo `cursor: String`. No es un problema en la práctica porque
`habilitado()` es una config fija por deploy (no cambia entre un request y
el siguiente de la misma sesión de búsqueda), pero un cliente no debería
asumir que el formato del cursor es estable entre ambientes.
"""
from __future__ import annotations

from typing import Optional

import strawberry

from . import db, search_engine
from .inputs import (
    FacetasDeclaracion,
    FacetasHechoJudicial,
    FiltroDeclaracion,
    FiltroHechoJudicial,
    OpcionConteo,
    Paginacion,
    PaginaDeclaraciones,
    PaginaHechosJudiciales,
)
from .mappers import to_declaracion, to_hecho_judicial, to_organizacion, to_persona
from .types import Declaracion, HechoJudicial, Organizacion, Persona


def _cursor_a_id(cursor: Optional[str]) -> Optional[int]:
    return int(cursor) if cursor else None


def _cursor_a_offset(cursor: Optional[str]) -> int:
    return int(cursor) if cursor else 0


# ---------------------------------------------------------------------------
# Construcción de filtros de Meilisearch (sintaxis: "campo = valor", con
# paréntesis + OR para "cualquiera de estos" y AND entre categorías distintas
# — la misma lógica OR-interno/AND-entre-facetas que se definió para el
# buscador). Los valores de texto van entre comillas dobles.
# ---------------------------------------------------------------------------

def _filtro_meilisearch_hj(filtro: FiltroHechoJudicial) -> str:
    partes = ['estado_publicacion = "publicado"']
    if filtro.categorias_delito_ids:
        or_ = " OR ".join(f"categoria_delito_id = {int(i)}" for i in filtro.categorias_delito_ids)
        partes.append(f"({or_})")
    if filtro.estados_judiciales:
        or_ = " OR ".join(f'estado_judicial = "{e.value}"' for e in filtro.estados_judiciales)
        partes.append(f"({or_})")
    if filtro.provincias_ids:
        or_ = " OR ".join(f"provincia_id = {int(i)}" for i in filtro.provincias_ids)
        partes.append(f"({or_})")
    if filtro.organizaciones_ids:
        or_ = " OR ".join(f"organizaciones_ids = {int(i)}" for i in filtro.organizaciones_ids)
        partes.append(f"({or_})")
    if filtro.fecha_desde:
        partes.append(f"fecha_hecho >= {search_engine.fecha_a_timestamp(filtro.fecha_desde)}")
    if filtro.fecha_hasta:
        partes.append(f"fecha_hecho <= {search_engine.fecha_a_timestamp(filtro.fecha_hasta)}")
    return " AND ".join(partes)


def _filtro_meilisearch_decl(filtro: FiltroDeclaracion) -> str:
    partes = ['estado_publicacion = "publicado"']
    if filtro.tipos:
        or_ = " OR ".join(f'tipo = "{t.value}"' for t in filtro.tipos)
        partes.append(f"({or_})")
    if filtro.provincias_ids:
        or_ = " OR ".join(f"provincia_id = {int(i)}" for i in filtro.provincias_ids)
        partes.append(f"({or_})")
    if filtro.organizaciones_ids:
        or_ = " OR ".join(f"organizaciones_ids = {int(i)}" for i in filtro.organizaciones_ids)
        partes.append(f"({or_})")
    if filtro.fecha_desde:
        partes.append(f"fecha >= {search_engine.fecha_a_timestamp(filtro.fecha_desde)}")
    if filtro.fecha_hasta:
        partes.append(f"fecha <= {search_engine.fecha_a_timestamp(filtro.fecha_hasta)}")
    return " AND ".join(partes)


@strawberry.type
class Query:
    @strawberry.field(description="Ficha de una persona por su código permanente (ej: PER-000123).")
    async def persona(self, info: strawberry.Info, codigo: str) -> Optional[Persona]:
        async with info.context["pool"].acquire() as con:
            fila = await db.fetch_persona_por_codigo(con, codigo)
        return to_persona(fila) if fila else None

    @strawberry.field(description="Ficha de una organización por su código permanente (ej: ORG-000123).")
    async def organizacion(self, info: strawberry.Info, codigo: str) -> Optional[Organizacion]:
        async with info.context["pool"].acquire() as con:
            fila = await db.fetch_organizacion_por_codigo(con, codigo)
        return to_organizacion(fila) if fila else None

    @strawberry.field(description="Ficha de un hecho judicial por su código permanente (ej: HJ-000123).")
    async def hecho_judicial(self, info: strawberry.Info, codigo: str) -> Optional[HechoJudicial]:
        async with info.context["pool"].acquire() as con:
            fila = await db.fetch_hecho_judicial_por_codigo(con, codigo)
        return to_hecho_judicial(fila) if fila else None

    @strawberry.field(description="Ficha de una declaración/voto por su código permanente (ej: DE-000123).")
    async def declaracion(self, info: strawberry.Info, codigo: str) -> Optional[Declaracion]:
        async with info.context["pool"].acquire() as con:
            fila = await db.fetch_declaracion_por_codigo(con, codigo)
        return to_declaracion(fila) if fila else None

    @strawberry.field(
        description="Búsqueda faceteada de hechos judiciales (funciona con o sin texto: los filtros solos alcanzan)."
    )
    async def buscar_hechos_judiciales(
        self,
        info: strawberry.Info,
        filtro: Optional[FiltroHechoJudicial] = None,
        paginacion: Optional[Paginacion] = None,
    ) -> PaginaHechosJudiciales:
        filtro = filtro or FiltroHechoJudicial()
        paginacion = paginacion or Paginacion()
        if search_engine.habilitado():
            return await _buscar_hj_meilisearch(info, filtro, paginacion)
        return await _buscar_hj_postgres(info, filtro, paginacion)

    @strawberry.field(description="Búsqueda faceteada de declaraciones y votos.")
    async def buscar_declaraciones(
        self,
        info: strawberry.Info,
        filtro: Optional[FiltroDeclaracion] = None,
        paginacion: Optional[Paginacion] = None,
    ) -> PaginaDeclaraciones:
        filtro = filtro or FiltroDeclaracion()
        paginacion = paginacion or Paginacion()
        if search_engine.habilitado():
            return await _buscar_decl_meilisearch(info, filtro, paginacion)
        return await _buscar_decl_postgres(info, filtro, paginacion)


# ---------------------------------------------------------------------------
# Implementación Meilisearch
# ---------------------------------------------------------------------------

async def _buscar_hj_meilisearch(
    info: strawberry.Info, filtro: FiltroHechoJudicial, paginacion: Paginacion
) -> PaginaHechosJudiciales:
    offset = _cursor_a_offset(paginacion.cursor)
    resultado = await search_engine.buscar(
        search_engine.INDICE_HECHOS_JUDICIALES,
        texto=filtro.texto,
        filtro=_filtro_meilisearch_hj(filtro),
        facetas=["categoria_delito_id", "estado_judicial", "provincia_id"],
        limite=paginacion.limite,
        offset=offset,
    )
    dataloaders = info.context["dataloaders"]
    ids = [int(hit["id"]) for hit in resultado.get("hits", [])]
    hidratados = await dataloaders.hecho_judicial_por_id.load_many(ids) if ids else []
    items = [h for h in hidratados if h is not None]  # por si el índice quedó desactualizado

    total = resultado.get("estimatedTotalHits", len(items))
    hay_mas = offset + len(ids) < total

    facet_dist = resultado.get("facetDistribution", {})
    categorias = []
    for id_str, cantidad in facet_dist.get("categoria_delito_id", {}).items():
        cat = await dataloaders.categoria_delito.load(int(id_str))
        categorias.append(OpcionConteo(valor=id_str, etiqueta=cat.nombre, cantidad=cantidad))
    estados = [
        OpcionConteo(valor=v, etiqueta=v, cantidad=c) for v, c in facet_dist.get("estado_judicial", {}).items()
    ]
    provincias = []
    for id_str, cantidad in facet_dist.get("provincia_id", {}).items():
        prov = await dataloaders.provincia.load(int(id_str))
        provincias.append(OpcionConteo(valor=id_str, etiqueta=prov.nombre, cantidad=cantidad))

    return PaginaHechosJudiciales(
        items=items,
        cursor_siguiente=str(offset + paginacion.limite) if hay_mas else None,
        hay_mas=hay_mas,
        total_aproximado=total,
        facetas=FacetasHechoJudicial(categorias_delito=categorias, estados_judiciales=estados, provincias=provincias),
    )


async def _buscar_decl_meilisearch(
    info: strawberry.Info, filtro: FiltroDeclaracion, paginacion: Paginacion
) -> PaginaDeclaraciones:
    offset = _cursor_a_offset(paginacion.cursor)
    resultado = await search_engine.buscar(
        search_engine.INDICE_DECLARACIONES,
        texto=filtro.texto,
        filtro=_filtro_meilisearch_decl(filtro),
        facetas=["tipo", "provincia_id"],
        limite=paginacion.limite,
        offset=offset,
    )
    dataloaders = info.context["dataloaders"]
    ids = [int(hit["id"]) for hit in resultado.get("hits", [])]
    hidratados = await dataloaders.declaracion_por_id.load_many(ids) if ids else []
    items = [d for d in hidratados if d is not None]

    total = resultado.get("estimatedTotalHits", len(items))
    hay_mas = offset + len(ids) < total

    facet_dist = resultado.get("facetDistribution", {})
    tipos = [OpcionConteo(valor=v, etiqueta=v, cantidad=c) for v, c in facet_dist.get("tipo", {}).items()]
    provincias = []
    for id_str, cantidad in facet_dist.get("provincia_id", {}).items():
        prov = await dataloaders.provincia.load(int(id_str))
        provincias.append(OpcionConteo(valor=id_str, etiqueta=prov.nombre, cantidad=cantidad))

    return PaginaDeclaraciones(
        items=items,
        cursor_siguiente=str(offset + paginacion.limite) if hay_mas else None,
        hay_mas=hay_mas,
        total_aproximado=total,
        facetas=FacetasDeclaracion(tipos=tipos, provincias=provincias),
    )


# ---------------------------------------------------------------------------
# Implementación Postgres (fallback de desarrollo / motor caído)
# ---------------------------------------------------------------------------

async def _buscar_hj_postgres(
    info: strawberry.Info, filtro: FiltroHechoJudicial, paginacion: Paginacion
) -> PaginaHechosJudiciales:
    cat_ids = [int(i) for i in filtro.categorias_delito_ids] if filtro.categorias_delito_ids else None
    org_ids = [int(i) for i in filtro.organizaciones_ids] if filtro.organizaciones_ids else None
    prov_ids = [int(i) for i in filtro.provincias_ids] if filtro.provincias_ids else None
    estados = [e.value for e in filtro.estados_judiciales] if filtro.estados_judiciales else None

    async with info.context["pool"].acquire() as con:
        filas = await db.search_hechos_judiciales(
            con,
            categorias_delito_ids=cat_ids,
            organizaciones_ids=org_ids,
            estados_judiciales=estados,
            provincias_ids=prov_ids,
            fecha_desde=filtro.fecha_desde,
            fecha_hasta=filtro.fecha_hasta,
            texto=filtro.texto,
            limite=paginacion.limite + 1,
            cursor_id=_cursor_a_id(paginacion.cursor),
        )
        hay_mas = len(filas) > paginacion.limite
        filas = filas[: paginacion.limite]

        conteos = await db.contar_facetas_hecho_judicial(
            con,
            organizaciones_ids=org_ids,
            provincias_ids=prov_ids,
            fecha_desde=filtro.fecha_desde,
            fecha_hasta=filtro.fecha_hasta,
            texto=filtro.texto,
        )
    total = sum(c["cantidad"] for c in conteos["categorias_delito"])

    return PaginaHechosJudiciales(
        items=[to_hecho_judicial(f) for f in filas],
        cursor_siguiente=str(filas[-1]["id"]) if filas and hay_mas else None,
        hay_mas=hay_mas,
        total_aproximado=total,
        facetas=FacetasHechoJudicial(
            categorias_delito=[
                OpcionConteo(valor=str(c["id"]), etiqueta=c["nombre"], cantidad=c["cantidad"])
                for c in conteos["categorias_delito"]
            ],
            estados_judiciales=[
                OpcionConteo(valor=c["valor"], etiqueta=c["valor"], cantidad=c["cantidad"])
                for c in conteos["estados_judiciales"]
            ],
            provincias=[
                OpcionConteo(valor=str(c["id"]), etiqueta=c["nombre"], cantidad=c["cantidad"])
                for c in conteos["provincias"]
            ],
        ),
    )


async def _buscar_decl_postgres(
    info: strawberry.Info, filtro: FiltroDeclaracion, paginacion: Paginacion
) -> PaginaDeclaraciones:
    org_ids = [int(i) for i in filtro.organizaciones_ids] if filtro.organizaciones_ids else None
    prov_ids = [int(i) for i in filtro.provincias_ids] if filtro.provincias_ids else None
    tipos = [tp.value for tp in filtro.tipos] if filtro.tipos else None

    async with info.context["pool"].acquire() as con:
        filas = await db.search_declaraciones(
            con,
            tipos=tipos,
            organizaciones_ids=org_ids,
            provincias_ids=prov_ids,
            fecha_desde=filtro.fecha_desde,
            fecha_hasta=filtro.fecha_hasta,
            texto=filtro.texto,
            limite=paginacion.limite + 1,
            cursor_id=_cursor_a_id(paginacion.cursor),
        )
        hay_mas = len(filas) > paginacion.limite
        filas = filas[: paginacion.limite]

        conteos = await db.contar_facetas_declaracion(
            con,
            organizaciones_ids=org_ids,
            provincias_ids=prov_ids,
            fecha_desde=filtro.fecha_desde,
            fecha_hasta=filtro.fecha_hasta,
            texto=filtro.texto,
        )
    total = sum(c["cantidad"] for c in conteos["tipos"])

    return PaginaDeclaraciones(
        items=[to_declaracion(f) for f in filas],
        cursor_siguiente=str(filas[-1]["id"]) if filas and hay_mas else None,
        hay_mas=hay_mas,
        total_aproximado=total,
        facetas=FacetasDeclaracion(
            tipos=[
                OpcionConteo(valor=c["valor"], etiqueta=c["valor"], cantidad=c["cantidad"]) for c in conteos["tipos"]
            ],
            provincias=[
                OpcionConteo(valor=str(c["id"]), etiqueta=c["nombre"], cantidad=c["cantidad"])
                for c in conteos["provincias"]
            ],
        ),
    )
