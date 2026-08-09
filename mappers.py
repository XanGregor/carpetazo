"""Convierte asyncpg.Record en instancias de los tipos de Strawberry (types.py). Compartido por dataloaders.py, queries.py y mutations.py para no duplicar el mapeo en cada lugar."""
from __future__ import annotations

import asyncpg

from . import types as t
from .enums import (
    EstadoJudicial,
    EstadoPublicacion,
    NivelFuente,
    TipoDeclaracion,
    TipoFinanciamiento,
    TipoOrganizacion,
)


def to_provincia(r: asyncpg.Record) -> t.Provincia:
    return t.Provincia(id=str(r["id"]), nombre=r["nombre"])


def to_rol(r: asyncpg.Record, id_key: str, nombre_key: str) -> t.RolEnHecho:
    return t.RolEnHecho(id=str(r[id_key]), nombre=r[nombre_key])


def to_categoria_delito(r: asyncpg.Record) -> t.CategoriaDelito:
    return t.CategoriaDelito(id=str(r["id"]), nombre=r["nombre"], categoria_padre_id=r["categoria_padre_id"])


def to_fuente(r: asyncpg.Record) -> t.Fuente:
    return t.Fuente(
        id=str(r["id"]),
        nivel=NivelFuente(r["nivel"]),
        tipo_documento=r["tipo_documento"],
        url=r["url"],
        medio_institucion=r["medio_institucion"],
        fecha_publicacion=r["fecha_publicacion"],
        hash_archivo=r["hash_archivo"],
    )


def to_financiamiento(r: asyncpg.Record) -> t.Financiamiento:
    return t.Financiamiento(id=str(r["id"]), tipo=TipoFinanciamiento(r["tipo"]), descripcion=r["descripcion"], fecha=r["fecha"])


def to_afiliacion(r: asyncpg.Record) -> t.Afiliacion:
    return t.Afiliacion(
        id=str(r["id"]),
        cargo=r["cargo"],
        fecha_inicio=r["fecha_inicio"],
        fecha_fin=r["fecha_fin"],
        persona_id=r["persona_id"],
        organizacion_id=r["organizacion_id"],
    )


def to_persona(r: asyncpg.Record) -> t.Persona:
    return t.Persona(
        id=str(r["id"]),
        codigo=r["codigo"],
        nombre_completo=r["nombre_completo"],
        alias=list(r["alias"]) if r["alias"] else [],
        fecha_nacimiento=r["fecha_nacimiento"],
        foto_url=r["foto_url"],
        bio=r["bio"],
        provincia_id=r["provincia_id"],
    )


def to_organizacion(r: asyncpg.Record) -> t.Organizacion:
    return t.Organizacion(
        id=str(r["id"]),
        codigo=r["codigo"],
        nombre=r["nombre"],
        tipo=TipoOrganizacion(r["tipo"]),
        descripcion=r["descripcion"],
        provincia_id=r["provincia_id"],
    )


def to_hecho_judicial(r: asyncpg.Record) -> t.HechoJudicial:
    return t.HechoJudicial(
        id=str(r["id"]),
        codigo=r["codigo"],
        titulo=r["titulo"],
        descripcion=r["descripcion"],
        estado_judicial=EstadoJudicial(r["estado_judicial"]),
        fecha_hecho=r["fecha_hecho"],
        estado_publicacion=EstadoPublicacion(r["estado_publicacion"]),
        categoria_delito_id=r["categoria_delito_id"],
        provincia_id=r["provincia_id"],
    )


def to_declaracion(r: asyncpg.Record) -> t.Declaracion:
    return t.Declaracion(
        id=str(r["id"]),
        codigo=r["codigo"],
        titulo=r["titulo"],
        descripcion=r["descripcion"],
        tipo=TipoDeclaracion(r["tipo"]),
        fecha=r["fecha"],
        estado_publicacion=EstadoPublicacion(r["estado_publicacion"]),
        provincia_id=r["provincia_id"],
    )
