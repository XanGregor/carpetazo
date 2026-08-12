"""
Punto de entrada de la API.

Monta dos endpoints de GraphQL:
  - /graphql            -> schema interno (con mutaciones; usa la web y las apps propias)
  - /api/publico/graphql -> schema público (solo lectura; para terceros con API key)

Correr en desarrollo:  uvicorn graphql_api.app:app --reload
Variables de entorno requeridas: DATABASE_URL, JWT_SECRET.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter

import logging

from . import db, search_engine, rate_limit
from .context import contexto_interno, contexto_publico
from .schema import schema_interno, schema_publico

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.get_pool()  # abre la pool de conexiones al arrancar
    try:
        await search_engine.configurar_indices()
    except Exception:
        # no tener Meilisearch arriba en el momento del deploy no debe
        # tumbar la API entera — queries.py cae al fallback de Postgres.
        logger.exception("No se pudo configurar los índices de Meilisearch al arrancar.")
    yield
    await rate_limit.cerrar_cliente()
    await db.close_pool()


app = FastAPI(title="Archivo de Corrupción — API", lifespan=lifespan)

app.include_router(
    GraphQLRouter(schema_interno, context_getter=contexto_interno),
    prefix="/graphql",
)
app.include_router(
    GraphQLRouter(schema_publico, context_getter=contexto_publico),
    prefix="/api/publico/graphql",
)
