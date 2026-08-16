"""
Test de carga/concurrencia para calibrar min_size/max_size del pool de
asyncpg (ver db.py, DB_POOL_MIN_SIZE / DB_POOL_MAX_SIZE).

Por qué hace falta esto y no alcanza con "probarlo a mano": el riesgo
concreto de este esquema no es el volumen de requests HTTP en sí, es que
GraphQL resuelve campos hermanos en paralelo y CADA resolver/DataLoader
saca su PROPIA conexión del pool (ver la nota repetida en dataloaders.py/
queries.py/mutations.py) — una sola query bien anidada (una ficha con
fuentes + personas + organizaciones + categoría + provincia + relaciones)
puede pedir 6 conexiones simultáneas por sí sola, sin que haya ningún otro
cliente pegándole a la API al mismo tiempo. Este script mide dos cosas
por separado para no confundirlas:

  1. "raw": tiempo de adquirir una conexión del pool + SELECT 1 + soltarla,
     bajo N pedidos concurrentes. Aísla el comportamiento puro del pool
     (cola de asyncpg cuando está agotado) de cualquier costo de GraphQL.
  2. "graphql": lo mismo pero disparando requests reales contra la app
     (vía ASGI in-process, sin levantar un servidor HTTP de verdad) — con
     dos queries de ejemplo: "simple" (1 conexión por request) y "ancha"
     (6 conexiones concurrentes POR REQUEST, para simular el peor caso de
     fan-out de una ficha completa).

asyncpg NUNCA falla un request por pool agotado (pool.acquire() espera en
cola, no lanza excepción salvo timeout explícito) — así que "calibrar" acá
no es buscar el tamaño que evita errores (con cualquier tamaño ≥1 el
sistema es correcto), es encontrar el tamaño que mantiene la latencia
razonable bajo la concurrencia esperada, sin gastar de más en conexiones
que Postgres tiene que mantener abiertas sin necesidad.

Uso:
    # Un solo tamaño de pool, la query ancha, concurrencia 20:
    python -m scripts.test_carga_pool --pool-max 10 --concurrencia 20 --query ancha

    # Barrido de varias combinaciones en una sola corrida (recrea el pool
    # entre cada combinación, ver db.get_pool()):
    python -m scripts.test_carga_pool --barrido

Requiere las mismas variables de entorno que la API (DATABASE_URL,
JWT_SECRET) — no requiere Meilisearch ni Redis (quedan deshabilitados con
su fallback normal si no están seteados; no son parte de lo que este
script mide).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

from graphql_api import db

QUERY_SIMPLE = 'query { persona(codigo: "PER-000001") { nombreCompleto } }'

# Deliberadamente "ancha": 6 campos de relación en un solo hecho, cada uno
# resuelto por un DataLoader distinto que saca su propia conexión del pool
# — el peor caso realista de fan-out por request (ver dataloaders.py).
QUERY_ANCHA = """
query {
  hechoJudicial(codigo: "HJ-000001") {
    titulo
    categoriaDelito { nombre }
    provincia { nombre }
    fuentes { nivel url }
    personas { rol { nombre } persona { nombreCompleto } }
    organizaciones { rol { nombre } organizacion { nombre } }
    relaciones { tipoRelacion descripcion }
  }
}
"""

QUERIES = {"simple": QUERY_SIMPLE, "ancha": QUERY_ANCHA}


@dataclass
class Resultado:
    duraciones_ok: list[float] = field(default_factory=list)
    errores: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.duraciones_ok) + len(self.errores)

    def percentil(self, p: float) -> float:
        if not self.duraciones_ok:
            return float("nan")
        datos = sorted(self.duraciones_ok)
        idx = min(int(len(datos) * p), len(datos) - 1)
        return datos[idx]


# ---------------------------------------------------------------------------
# Modo "raw": solo el pool de asyncpg, sin GraphQL/HTTP de por medio
# ---------------------------------------------------------------------------

async def _un_acquire_raw(pool) -> tuple[float, str | None]:
    inicio = time.perf_counter()
    try:
        async with pool.acquire() as con:
            await con.fetchval("SELECT 1")
        return time.perf_counter() - inicio, None
    except Exception as e:  # noqa: BLE001 — acá interesa contar cualquier fallo, no distinguir tipos
        return time.perf_counter() - inicio, repr(e)


async def _oleada_raw(pool, concurrencia: int) -> Resultado:
    salidas = await asyncio.gather(*(_un_acquire_raw(pool) for _ in range(concurrencia)))
    resultado = Resultado()
    for duracion, error in salidas:
        if error is None:
            resultado.duraciones_ok.append(duracion)
        else:
            resultado.errores.append(error)
    return resultado


# ---------------------------------------------------------------------------
# Modo "graphql": requests reales contra la app, in-process vía ASGI
# ---------------------------------------------------------------------------

async def _un_request_graphql(client: httpx.AsyncClient, query: str) -> tuple[float, str | None]:
    inicio = time.perf_counter()
    try:
        r = await client.post("/graphql", json={"query": query})
        duracion = time.perf_counter() - inicio
        if r.status_code != 200:
            return duracion, f"HTTP {r.status_code}"
        cuerpo = r.json()
        if cuerpo.get("errors"):
            return duracion, str(cuerpo["errors"])[:200]
        return duracion, None
    except Exception as e:  # noqa: BLE001
        return time.perf_counter() - inicio, repr(e)


async def _oleada_graphql(client: httpx.AsyncClient, query: str, concurrencia: int) -> Resultado:
    salidas = await asyncio.gather(*(_un_request_graphql(client, query) for _ in range(concurrencia)))
    resultado = Resultado()
    for duracion, error in salidas:
        if error is None:
            resultado.duraciones_ok.append(duracion)
        else:
            resultado.errores.append(error)
    return resultado


# ---------------------------------------------------------------------------
# Orquestación de una corrida (N oleadas de `concurrencia` en paralelo,
# hasta sumar `total` requests) y de un barrido de configuraciones
# ---------------------------------------------------------------------------

async def _recrear_pool(pool_min: int, pool_max: int):
    """
    Cierra el pool actual (si existe) y crea uno nuevo con el tamaño
    pedido — lee DB_POOL_MIN_SIZE/DB_POOL_MAX_SIZE en el momento de crear
    el pool (ver db.get_pool()), así que alcanza con setear las env vars
    antes de llamarlo. Permite comparar varios tamaños en una sola
    corrida del script sin relanzar el proceso.
    """
    await db.close_pool()
    os.environ["DB_POOL_MIN_SIZE"] = str(pool_min)
    os.environ["DB_POOL_MAX_SIZE"] = str(pool_max)
    return await db.get_pool()


async def _correr_raw(pool, *, concurrencia: int, total: int) -> Resultado:
    acumulado = Resultado()
    oleadas = (total + concurrencia - 1) // concurrencia
    for _ in range(oleadas):
        r = await _oleada_raw(pool, concurrencia)
        acumulado.duraciones_ok.extend(r.duraciones_ok)
        acumulado.errores.extend(r.errores)
    return acumulado


async def _correr_graphql(*, concurrencia: int, total: int, query_tipo: str) -> Resultado:
    from graphql_api.app import app  # import diferido: dispara la carga del schema recién acá

    query = QUERIES[query_tipo]
    acumulado = Resultado()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        oleadas = (total + concurrencia - 1) // concurrencia
        for _ in range(oleadas):
            r = await _oleada_graphql(client, query, concurrencia)
            acumulado.duraciones_ok.extend(r.duraciones_ok)
            acumulado.errores.extend(r.errores)
    return acumulado


def _imprimir_resumen(titulo: str, resultado: Resultado) -> None:
    print(f"  {titulo}")
    print(f"    requests: {resultado.total}  ok: {len(resultado.duraciones_ok)}  errores: {len(resultado.errores)}")
    if resultado.duraciones_ok:
        p50 = resultado.percentil(0.50) * 1000
        p95 = resultado.percentil(0.95) * 1000
        p99 = resultado.percentil(0.99) * 1000
        maximo = max(resultado.duraciones_ok) * 1000
        promedio = statistics.mean(resultado.duraciones_ok) * 1000
        print(f"    latencia (ms): p50={p50:.1f} p95={p95:.1f} p99={p99:.1f} max={maximo:.1f} promedio={promedio:.1f}")
    if resultado.errores:
        print(f"    primer error: {resultado.errores[0][:200]}")


async def _correr_una_configuracion(*, pool_min: int, pool_max: int, concurrencia: int, total: int, modo: str) -> None:
    pool = await _recrear_pool(pool_min, pool_max)
    print(f"pool min={pool_min} max={pool_max} | concurrencia={concurrencia} | total={total}")

    # Arranque en frío: asyncpg abre min_size conexiones al crear el pool,
    # pero las conexiones por encima de eso se abren recién bajo demanda
    # (TCP + fork del backend en Postgres) — la primera ráfaga que supera
    # min_size paga ese costo de una sola vez. Se mide APARTE del resto
    # (que corre después, con el pool ya "caliente") porque si no
    # contamina la comparación: en el sandbox de desarrollo se midió un
    # burst de 20 contra un pool recién creado en ~400ms, contra ~4ms
    # para la misma ráfaga con el pool ya con las conexiones abiertas —
    # dos órdenes de magnitud de diferencia, no ruido. Ver README.
    arranque_en_frio = await _oleada_raw(pool, min(concurrencia, pool_max))
    if arranque_en_frio.duraciones_ok:
        print(
            f"  arranque en frío (primera ráfaga tras crear el pool): "
            f"max={max(arranque_en_frio.duraciones_ok) * 1000:.1f}ms "
            f"(fuerza abrir hasta {min(concurrencia, pool_max)} conexiones físicas nuevas)"
        )

    if modo in ("raw", "ambos"):
        resultado = await _correr_raw(pool, concurrencia=concurrencia, total=total)
        _imprimir_resumen("raw en régimen estable (pool ya con las conexiones abiertas)", resultado)

    if modo in ("graphql", "ambos"):
        for query_tipo in ("simple", "ancha"):
            resultado = await _correr_graphql(concurrencia=concurrencia, total=total, query_tipo=query_tipo)
            _imprimir_resumen(f"graphql en régimen estable, query={query_tipo}", resultado)
    print()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Test de carga/concurrencia para calibrar el pool de asyncpg.")
    parser.add_argument("--pool-min", type=int, default=2)
    parser.add_argument("--pool-max", type=int, default=10)
    parser.add_argument("--concurrencia", type=int, default=20, help="Requests simultáneos por oleada.")
    parser.add_argument("--total", type=int, default=100, help="Total de requests a lanzar (en oleadas de --concurrencia).")
    parser.add_argument("--query", choices=["simple", "ancha"], default="ancha")
    parser.add_argument("--modo", choices=["raw", "graphql", "ambos"], default="ambos")
    parser.add_argument(
        "--barrido", action="store_true",
        help="Ignora --pool-max/--concurrencia y corre una matriz fija de combinaciones representativas.",
    )
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("Falta DATABASE_URL.", file=sys.stderr)
        return 1

    try:
        if args.barrido:
            for pool_max in (5, 10, 20):
                for concurrencia in (5, 20, 50):
                    await _correr_una_configuracion(
                        pool_min=min(args.pool_min, pool_max), pool_max=pool_max,
                        concurrencia=concurrencia, total=max(args.total, concurrencia * 3),
                        modo="ambos",
                    )
        else:
            await _correr_una_configuracion(
                pool_min=args.pool_min, pool_max=args.pool_max,
                concurrencia=args.concurrencia, total=args.total, modo=args.modo,
            )
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
