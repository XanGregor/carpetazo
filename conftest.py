"""
Fixtures compartidas. La clave es la fixture `con`: abre una conexión,
arranca una transacción, y al terminar el test hace ROLLBACK — así cada
test parte de la base tal como quedó tras correr archivo_corrupcion_schema.sql
(con los datos semilla) y nunca deja basura para el siguiente test, sin
necesidad de un TRUNCATE manual en cada uno.

Las mutations de graphql_api usan `async with con.transaction():` puertas
adentro — como ya estamos dentro de una transacción de la fixture, asyncpg
convierte esos bloques en SAVEPOINTs automáticamente. Si algo falla ahí
adentro, revierte solo esa parte, no todo el test.

Requiere: PostgreSQL corriendo en localhost con la base
archivo_corrupcion_test ya inicializada con archivo_corrupcion_schema.sql,
y variables de entorno DATABASE_URL / JWT_SECRET seteadas (ver README).
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost/archivo_corrupcion_test")
os.environ.setdefault("JWT_SECRET", "test-secret-no-usar-en-produccion")

import asyncio
import hashlib
import secrets
from contextlib import asynccontextmanager

import asyncpg
import pytest_asyncio

from graphql_api.auth import UsuarioActual
from graphql_api.dataloaders import Loaders
from graphql_api.enums import RolUsuario


class _PoolDeUnaConexion:
    """
    La app real usa un asyncpg.Pool para que cada resolver/DataLoader saque
    su propia conexión (necesario porque GraphQL puede resolver varios campos
    en paralelo). En los tests, en cambio, queremos que TODO pase por la misma
    conexión — la de la fixture `con`, ya dentro de una transacción que se
    revierte al final — para que el rollback-based cleanup funcione y para
    que los datos insertados en el test sean visibles dentro de la misma
    transacción sin commitear nada de verdad.

    Envolver el acceso en un asyncio.Lock serializa esas llamadas
    concurrentes en vez de dejarlas chocar contra la limitación real de
    asyncpg (una conexión no soporta dos queries al mismo tiempo). Es más
    lento que una pool real, pero correcto — y en un test eso es lo que
    importa.
    """

    def __init__(self, con: asyncpg.Connection):
        self._con = con
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self):
        async with self._lock:
            yield self._con


@pytest_asyncio.fixture
async def con():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _sembrar_usuarios_de_test():
    """
    Corre UNA vez por sesión de tests, con su propia conexión y COMMIT real
    (no la conexión con rollback de la fixture `con`) — si los usuarios de
    prueba vivieran dentro de una transacción que se revierte, no
    existirían para el siguiente test. Es idempotente (ON CONFLICT DO
    NOTHING), así que correr la suite varias veces contra la misma base no
    duplica filas.
    """
    from graphql_api.auth import hash_password

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        for email, rol in [
            ("admin@test.com", "admin"),
            ("editor@test.com", "editor"),
            ("colaborador@test.com", "colaborador"),
        ]:
            await conn.execute(
                "INSERT INTO usuario (nombre, email, password_hash, rol) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (email) DO NOTHING",
                rol.capitalize() + " de prueba", email, hash_password("password123"), rol,
            )
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def usuarios(con) -> dict[str, UsuarioActual]:
    filas = await con.fetch("SELECT id, email, rol FROM usuario ORDER BY id")
    return {f["rol"]: UsuarioActual(id=f["id"], email=f["email"], rol=RolUsuario(f["rol"])) for f in filas}


@pytest_asyncio.fixture
async def ids_semilla(con) -> dict[str, int]:
    categoria = await con.fetchrow("SELECT id FROM categoria_delito WHERE nombre = 'Cohecho y soborno'")
    categoria2 = await con.fetchrow("SELECT id FROM categoria_delito WHERE nombre = 'Lavado de activos'")
    provincia = await con.fetchrow("SELECT id FROM provincia WHERE nombre = 'Mendoza'")
    provincia2 = await con.fetchrow("SELECT id FROM provincia WHERE nombre = 'Buenos Aires'")
    rol_acusado = await con.fetchrow("SELECT id FROM rol_en_hecho WHERE nombre = 'acusado'")
    rol_denunciante = await con.fetchrow("SELECT id FROM rol_en_hecho WHERE nombre = 'denunciante'")
    return {
        "categoria_delito_id": categoria["id"],
        "categoria_delito_id_2": categoria2["id"],
        "provincia_id": provincia["id"],
        "provincia_id_2": provincia2["id"],
        "rol_acusado_id": rol_acusado["id"],
        "rol_denunciante_id": rol_denunciante["id"],
    }


def contexto(con, usuario: UsuarioActual | None = None, api_key=None) -> dict:
    pool = _PoolDeUnaConexion(con)
    ctx = {"pool": pool, "dataloaders": Loaders(pool)}
    if usuario is not None:
        ctx["usuario"] = usuario
    if api_key is not None:
        ctx["api_key"] = api_key
    return ctx


@pytest_asyncio.fixture
async def api_key_temporal():
    """
    Factory fixture para crear filas REALES de `api_key` (COMMIT real, con
    su propia conexión — no la de la fixture `con`, que se revierte al
    final del test).

    Por qué hace falta esto y `con`/`contexto()` no alcanzan: los tests
    end-to-end que pegan por HTTP real contra la app (`TestClient(app)`,
    ver test_rate_limit_e2e.py y test_extensions_e2e.py) usan el pool de
    conexiones REAL de la aplicación (`db.get_pool()`), no la conexión de
    la fixture `con` — así que una fila insertada dentro de la transacción
    de `con` es invisible para esos tests hasta que se revierte (nunca se
    commitea). Se necesita una API key que exista de verdad en la base
    para que `auth.validar_api_key` la encuentre desde esa otra conexión.

    Se borra a mano al terminar el test (no hay transacción que revertir).
    Devuelve una función factory en vez de una sola key para poder crear
    más de una por test con distintos límites (ver el caso de "misma query,
    dos api keys con límite de profundidad distinto").
    """
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    creadas: list[int] = []

    async def _crear(*, rate_limit_por_minuto: int = 1000, limite_profundidad_query: int = 10) -> str:
        key_plana = f"acp_test_{secrets.token_urlsafe(16)}"
        key_hash = hashlib.sha256(key_plana.encode("utf-8")).hexdigest()
        fila = await conn.fetchrow(
            "INSERT INTO api_key (nombre, email, uso_previsto, key_hash, rate_limit_por_minuto, limite_profundidad_query) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            "API key de test (e2e)", "e2e@test.local", "tests automatizados", key_hash,
            rate_limit_por_minuto, limite_profundidad_query,
        )
        creadas.append(fila["id"])
        return key_plana

    try:
        yield _crear
    finally:
        if creadas:
            await conn.execute("DELETE FROM api_key WHERE id = ANY($1::bigint[])", creadas)
        await conn.close()
