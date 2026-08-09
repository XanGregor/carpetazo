"""
Un DataLoader por cada relación resoluble desde GraphQL. Se instancian
TODOS juntos, una vez por request, dentro de la clase Loaders — así, si
una query pide 50 hechos judiciales con sus fuentes y sus personas
involucradas, cada relación se resuelve con UNA query batched, no 100.

Cada batch load_fn saca su propia conexión del pool con
`self.pool.acquire()` y la devuelve al terminar. Esto es necesario porque
GraphQL resuelve campos hermanos en paralelo — si dos DataLoaders
distintos compartieran una sola conexión, chocarían entre sí (asyncpg no
soporta dos queries concurrentes en la misma conexión).

Los loaders "por id propio" (persona, organizacion, hecho_judicial_por_id,
declaracion_por_id) también se reutilizan como building blocks de otros
loaders más complejos (ver _cargar_relaciones_por_hecho), en vez de volver
a golpear la base de datos.
"""
from __future__ import annotations

from typing import Optional

import asyncpg
from strawberry.dataloader import DataLoader

from . import db
from . import types as t
from .enums import TipoRelacionHecho
from .mappers import (
    to_afiliacion,
    to_categoria_delito,
    to_declaracion,
    to_financiamiento,
    to_fuente,
    to_hecho_judicial,
    to_organizacion,
    to_persona,
    to_provincia,
    to_rol,
)


class Loaders:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

        self.provincia = DataLoader(load_fn=self._cargar_provincias)
        self.categoria_delito = DataLoader(load_fn=self._cargar_categorias)
        self.persona = DataLoader(load_fn=self._cargar_personas)
        self.organizacion = DataLoader(load_fn=self._cargar_organizaciones)
        self.hecho_judicial_por_id = DataLoader(load_fn=self._cargar_hechos_judiciales)
        self.declaracion_por_id = DataLoader(load_fn=self._cargar_declaraciones)

        self.afiliaciones_por_persona = DataLoader(load_fn=self._cargar_afiliaciones_por_persona)
        self.afiliaciones_por_organizacion = DataLoader(load_fn=self._cargar_afiliaciones_por_organizacion)
        self.financiamiento_por_organizacion = DataLoader(load_fn=self._cargar_financiamiento)

        self.fuentes_por_hecho_judicial = DataLoader(load_fn=self._make_fuentes_loader("hecho_judicial_id"))
        self.fuentes_por_declaracion = DataLoader(load_fn=self._make_fuentes_loader("declaracion_id"))
        self.fuentes_por_financiamiento = DataLoader(load_fn=self._make_fuentes_loader("financiamiento_id"))

        self.personas_por_hecho_judicial = DataLoader(load_fn=self._make_personas_en_hecho_loader("hecho_judicial"))
        self.personas_por_declaracion = DataLoader(load_fn=self._make_personas_en_hecho_loader("declaracion"))
        self.organizaciones_por_hecho_judicial = DataLoader(load_fn=self._make_organizaciones_en_hecho_loader("hecho_judicial"))
        self.organizaciones_por_declaracion = DataLoader(load_fn=self._make_organizaciones_en_hecho_loader("declaracion"))

        self.hechos_judiciales_por_persona = DataLoader(load_fn=self._make_hechos_por_persona_loader("hecho_judicial"))
        self.declaraciones_por_persona = DataLoader(load_fn=self._make_hechos_por_persona_loader("declaracion"))
        self.hechos_judiciales_por_organizacion = DataLoader(load_fn=self._make_hechos_por_organizacion_loader("hecho_judicial"))
        self.declaraciones_por_organizacion = DataLoader(load_fn=self._make_hechos_por_organizacion_loader("declaracion"))

        self.relaciones_por_hecho = DataLoader(load_fn=self._cargar_relaciones_por_hecho)

    # -- loaders "por id propio" ------------------------------------------

    async def _cargar_provincias(self, ids: list[int]) -> list[t.Provincia]:
        async with self.pool.acquire() as con:
            filas = await db.fetch_por_ids(con, "provincia", ids)
        por_id = {f["id"]: to_provincia(f) for f in filas}
        return [por_id[i] for i in ids]

    async def _cargar_categorias(self, ids: list[int]) -> list[t.CategoriaDelito]:
        async with self.pool.acquire() as con:
            filas = await db.fetch_por_ids(con, "categoria_delito", ids)
        por_id = {f["id"]: to_categoria_delito(f) for f in filas}
        return [por_id[i] for i in ids]

    async def _cargar_personas(self, ids: list[int]) -> list[t.Persona]:
        async with self.pool.acquire() as con:
            filas = await db.fetch_por_ids(con, "persona", ids)
        por_id = {f["id"]: to_persona(f) for f in filas}
        return [por_id[i] for i in ids]

    async def _cargar_organizaciones(self, ids: list[int]) -> list[t.Organizacion]:
        async with self.pool.acquire() as con:
            filas = await db.fetch_por_ids(con, "organizacion", ids)
        por_id = {f["id"]: to_organizacion(f) for f in filas}
        return [por_id[i] for i in ids]

    async def _cargar_hechos_judiciales(self, ids: list[int]) -> list[Optional[t.HechoJudicial]]:
        async with self.pool.acquire() as con:
            filas = await db.fetch_por_ids(con, "hecho_judicial", ids)
        por_id = {f["id"]: to_hecho_judicial(f) for f in filas}
        return [por_id.get(i) for i in ids]

    async def _cargar_declaraciones(self, ids: list[int]) -> list[Optional[t.Declaracion]]:
        async with self.pool.acquire() as con:
            filas = await db.fetch_por_ids(con, "declaracion", ids)
        por_id = {f["id"]: to_declaracion(f) for f in filas}
        return [por_id.get(i) for i in ids]

    # -- loaders uno-a-muchos ----------------------------------------------

    async def _cargar_afiliaciones_por_persona(self, persona_ids: list[int]) -> list[list[t.Afiliacion]]:
        async with self.pool.acquire() as con:
            agrupado = await db.batch_afiliaciones_por_persona(con, persona_ids)
        return [[to_afiliacion(f) for f in agrupado[pid]] for pid in persona_ids]

    async def _cargar_afiliaciones_por_organizacion(self, organizacion_ids: list[int]) -> list[list[t.Afiliacion]]:
        async with self.pool.acquire() as con:
            agrupado = await db.batch_afiliaciones_por_organizacion(con, organizacion_ids)
        return [[to_afiliacion(f) for f in agrupado[oid]] for oid in organizacion_ids]

    async def _cargar_financiamiento(self, organizacion_ids: list[int]) -> list[list[t.Financiamiento]]:
        async with self.pool.acquire() as con:
            agrupado = await db.batch_financiamiento_por_organizacion(con, organizacion_ids)
        return [[to_financiamiento(f) for f in agrupado[oid]] for oid in organizacion_ids]

    def _make_fuentes_loader(self, columna_fk: str):
        async def cargar(ids: list[int]) -> list[list[t.Fuente]]:
            async with self.pool.acquire() as con:
                agrupado = await db.batch_fuentes(con, columna_fk, ids)
            return [[to_fuente(f) for f in agrupado[i]] for i in ids]

        return cargar

    def _make_personas_en_hecho_loader(self, hecho_tipo: str):
        async def cargar(hecho_ids: list[int]) -> list[list[t.PersonaEnHecho]]:
            async with self.pool.acquire() as con:
                agrupado = await db.batch_vinculos_persona(con, hecho_tipo, hecho_ids)
            return [
                [t.PersonaEnHecho(rol=to_rol(f, "rol_id", "rol_nombre"), persona=to_persona(f)) for f in agrupado[i]]
                for i in hecho_ids
            ]

        return cargar

    def _make_organizaciones_en_hecho_loader(self, hecho_tipo: str):
        async def cargar(hecho_ids: list[int]) -> list[list[t.OrganizacionEnHecho]]:
            async with self.pool.acquire() as con:
                agrupado = await db.batch_vinculos_organizacion(con, hecho_tipo, hecho_ids)
            return [
                [
                    t.OrganizacionEnHecho(rol=to_rol(f, "rol_id", "rol_nombre"), organizacion=to_organizacion(f))
                    for f in agrupado[i]
                ]
                for i in hecho_ids
            ]

        return cargar

    def _make_hechos_por_persona_loader(self, hecho_tipo: str):
        mapper = to_hecho_judicial if hecho_tipo == "hecho_judicial" else to_declaracion

        async def cargar(persona_ids: list[int]) -> list[list]:
            async with self.pool.acquire() as con:
                agrupado = await db.batch_hechos_por_persona(con, hecho_tipo, persona_ids)
            return [[mapper(f) for f in agrupado[i]] for i in persona_ids]

        return cargar

    def _make_hechos_por_organizacion_loader(self, hecho_tipo: str):
        mapper = to_hecho_judicial if hecho_tipo == "hecho_judicial" else to_declaracion

        async def cargar(organizacion_ids: list[int]) -> list[list]:
            async with self.pool.acquire() as con:
                agrupado = await db.batch_hechos_por_organizacion(con, hecho_tipo, organizacion_ids)
            return [[mapper(f) for f in agrupado[i]] for i in organizacion_ids]

        return cargar

    # -- relaciones entre hechos: resuelve el "otro lado" reutilizando los
    #    loaders por id, para no duplicar el fetch de hecho_judicial/declaracion
    async def _cargar_relaciones_por_hecho(
        self, claves: list[tuple[str, int]]
    ) -> list[list[t.HechoRelacionado]]:
        async with self.pool.acquire() as con:
            agrupado = await db.batch_relaciones_por_hecho(con, claves)

        pendientes_judicial: set[int] = set()
        pendientes_declaracion: set[int] = set()
        for clave in claves:
            for rel in agrupado[clave]:
                otro_tipo, otro_id = self._otro_lado(clave, rel)
                (pendientes_judicial if otro_tipo == "hecho_judicial" else pendientes_declaracion).add(otro_id)

        judiciales: dict[int, Optional[t.HechoJudicial]] = {}
        declaraciones: dict[int, Optional[t.Declaracion]] = {}
        if pendientes_judicial:
            ids = list(pendientes_judicial)
            resultados = await self.hecho_judicial_por_id.load_many(ids)
            judiciales = dict(zip(ids, resultados))
        if pendientes_declaracion:
            ids = list(pendientes_declaracion)
            resultados = await self.declaracion_por_id.load_many(ids)
            declaraciones = dict(zip(ids, resultados))

        salida: list[list[t.HechoRelacionado]] = []
        for clave in claves:
            items: list[t.HechoRelacionado] = []
            for rel in agrupado[clave]:
                otro_tipo, otro_id = self._otro_lado(clave, rel)
                hecho = judiciales.get(otro_id) if otro_tipo == "hecho_judicial" else declaraciones.get(otro_id)
                if hecho is None:
                    continue  # el otro lado no existe o no está publicado
                items.append(
                    t.HechoRelacionado(
                        tipo_relacion=TipoRelacionHecho(rel["tipo_relacion"]),
                        descripcion=rel["descripcion"],
                        hecho=hecho,
                    )
                )
            salida.append(items)
        return salida

    @staticmethod
    def _otro_lado(clave: tuple[str, int], rel: asyncpg.Record) -> tuple[str, int]:
        origen = (rel["origen_tipo"], rel["origen_id"])
        return (rel["destino_tipo"], rel["destino_id"]) if origen == clave else origen
