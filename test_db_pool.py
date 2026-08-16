"""
Test de regresión puntual para el bug real que encontró
scripts/test_carga_pool.py: sin el lock en db.get_pool(), varias llamadas
concurrentes contra un pool recién frío (`_pool is None`) podían crear
más de un pool — cada una pasaba el chequeo `if _pool is None` antes de
que la primera terminara de asignar el resultado a la variable global.
Los pools "perdedores" de esa carrera quedaban huérfanos (nada volvía a
referenciarlos, así que sus conexiones nunca se cerraban) — en la
práctica esto se manifestaba como `TooManyConnectionsError: sorry, too
many clients already` después de suficientes ráfagas contra un pool frío.

Se confirmó de verdad antes de corregirlo: 20 llamadas concurrentes a
get_pool() sin el lock creaban 20 pools distintos; con el lock, 1.
"""
import asyncio

import pytest

from graphql_api import db


@pytest.mark.asyncio
async def test_llamadas_concurrentes_a_get_pool_devuelven_el_mismo_pool():
    await db.close_pool()  # asegura arrancar de un estado "frío" (_pool is None)
    try:
        pools = await asyncio.gather(*(db.get_pool() for _ in range(20)))
        assert len({id(p) for p in pools}) == 1, (
            "get_pool() concurrente creó más de un pool — la condición de "
            "carrera que se corrigió en esta tanda volvió a aparecer."
        )
    finally:
        await db.close_pool()
