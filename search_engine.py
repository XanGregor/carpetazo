"""
Sincronización con Meilisearch: el motor de búsqueda/faceteo que usa
buscar_hechos_judiciales/buscar_declaraciones (ver queries.py) cuando está
configurado. Sin MEILISEARCH_URL, todas las funciones acá son no-ops y
queries.py cae al fallback de Postgres (ILIKE + GROUP BY) — así el
proyecto sigue andando en desarrollo local sin tener Meilisearch corriendo.

Por qué REST directo en vez del paquete oficial `meilisearch`: ese paquete
es sync-only (bloquearía el event loop de FastAPI/Strawberry si lo
llamáramos tal cual desde un resolver async). Su API REST es simple y
estable, así que hablarle directo con httpx.AsyncClient es más prolijo
que meter cada llamada en un threadpool con asyncio.to_thread.

Consistencia: cada sincronización es "fire and forget" respecto a la
indexación real — Meilisearch procesa el documento de forma asíncrona
internamente (task interno), así que hay una ventana de consistencia
eventual de milisegundos a pocos segundos entre "se aprobó el hecho" y
"aparece en la búsqueda". Aceptable para este caso de uso.

Postgres sigue siendo la fuente de verdad: buscar_* usa Meilisearch para
obtener QUÉ ids matchean + los conteos por faceta, pero los objetos que
se devuelven al cliente GraphQL se hidratan desde Postgres vía los
DataLoaders existentes — así un documento desactualizado en el índice
nunca termina mostrando datos viejos.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Optional

import asyncpg
import httpx

from . import db

INDICE_HECHOS_JUDICIALES = "hechos_judiciales"
INDICE_DECLARACIONES = "declaraciones"


def _url() -> Optional[str]:
    return os.environ.get("MEILISEARCH_URL")


def _api_key() -> Optional[str]:
    return os.environ.get("MEILISEARCH_API_KEY")


def habilitado() -> bool:
    return bool(_url())


def _cliente() -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {_api_key()}"} if _api_key() else {}
    return httpx.AsyncClient(base_url=_url(), headers=headers, timeout=5.0)


def fecha_a_timestamp(d: Optional[date]) -> Optional[int]:
    if d is None:
        return None
    return int(datetime(d.year, d.month, d.day).timestamp())


# ---------------------------------------------------------------------------
# Configuración de índices — se corre una vez (al desplegar), no por request
# ---------------------------------------------------------------------------

async def configurar_indices() -> None:
    if not habilitado():
        return
    async with _cliente() as c:
        await c.patch(
            f"/indexes/{INDICE_HECHOS_JUDICIALES}/settings",
            json={
                "searchableAttributes": ["titulo", "descripcion", "personas_nombres", "organizaciones_nombres"],
                "filterableAttributes": [
                    "categoria_delito_id", "estado_judicial", "provincia_id",
                    "organizaciones_ids", "fecha_hecho", "estado_publicacion",
                ],
                "sortableAttributes": ["fecha_hecho"],
            },
        )
        await c.patch(
            f"/indexes/{INDICE_DECLARACIONES}/settings",
            json={
                "searchableAttributes": ["titulo", "descripcion", "personas_nombres", "organizaciones_nombres"],
                "filterableAttributes": ["tipo", "provincia_id", "organizaciones_ids", "fecha", "estado_publicacion"],
                "sortableAttributes": ["fecha"],
            },
        )


# ---------------------------------------------------------------------------
# Documentos: Postgres (denormalizado) -> forma que indexa Meilisearch
# ---------------------------------------------------------------------------

def _documento_hecho_judicial(fila: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(fila["id"]),
        "codigo": fila["codigo"],
        "titulo": fila["titulo"],
        "descripcion": fila["descripcion"],
        "categoria_delito_id": fila["categoria_delito_id"],
        "estado_judicial": fila["estado_judicial"],
        "fecha_hecho": fecha_a_timestamp(fila["fecha_hecho"]),
        "provincia_id": fila["provincia_id"],
        "organizaciones_ids": list(fila["organizaciones_ids"] or []),
        "organizaciones_nombres": list(fila["organizaciones_nombres"] or []),
        "personas_nombres": list(fila["personas_nombres"] or []),
        "estado_publicacion": fila["estado_publicacion"],
    }


def _documento_declaracion(fila: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(fila["id"]),
        "codigo": fila["codigo"],
        "titulo": fila["titulo"],
        "descripcion": fila["descripcion"],
        "tipo": fila["tipo"],
        "fecha": fecha_a_timestamp(fila["fecha"]),
        "provincia_id": fila["provincia_id"],
        "organizaciones_ids": list(fila["organizaciones_ids"] or []),
        "organizaciones_nombres": list(fila["organizaciones_nombres"] or []),
        "personas_nombres": list(fila["personas_nombres"] or []),
        "estado_publicacion": fila["estado_publicacion"],
    }


# ---------------------------------------------------------------------------
# Operaciones de bajo nivel contra la API REST
# ---------------------------------------------------------------------------

async def _indexar(indice: str, documento: dict[str, Any]) -> None:
    async with _cliente() as c:
        await c.post(f"/indexes/{indice}/documents", params={"primaryKey": "id"}, json=[documento])


async def _eliminar(indice: str, id_: str) -> None:
    async with _cliente() as c:
        await c.delete(f"/indexes/{indice}/documents/{id_}")


async def buscar(
    indice: str,
    *,
    texto: Optional[str],
    filtro: Optional[str],
    facetas: list[str],
    limite: int,
    offset: int,
) -> dict[str, Any]:
    async with _cliente() as c:
        body: dict[str, Any] = {"q": texto or "", "limit": limite, "offset": offset, "facets": facetas}
        if filtro:
            body["filter"] = filtro
        resp = await c.post(f"/indexes/{indice}/search", json=body)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Sincronización — llamar después de cada commit en mutations.py
# ---------------------------------------------------------------------------

async def sincronizar_hecho_judicial(con: asyncpg.Connection, hecho_id: int) -> None:
    """Re-indexa el hecho si está publicado; lo saca del índice si no lo está (borrador/pendiente/rechazado)."""
    if not habilitado():
        return
    fila = await db.obtener_vista_busqueda_hecho_judicial(con, hecho_id)
    if fila is None:
        return
    if fila["estado_publicacion"] != "publicado":
        await _eliminar(INDICE_HECHOS_JUDICIALES, str(hecho_id))
        return
    await _indexar(INDICE_HECHOS_JUDICIALES, _documento_hecho_judicial(fila))


async def sincronizar_declaracion(con: asyncpg.Connection, declaracion_id: int) -> None:
    if not habilitado():
        return
    fila = await db.obtener_vista_busqueda_declaracion(con, declaracion_id)
    if fila is None:
        return
    if fila["estado_publicacion"] != "publicado":
        await _eliminar(INDICE_DECLARACIONES, str(declaracion_id))
        return
    await _indexar(INDICE_DECLARACIONES, _documento_declaracion(fila))
