# Changelog

Todos los cambios notables al backend de **Archivo de Corrupción** se
documentan acá, en orden cronológico (la primera sección es la más
reciente). El proyecto todavía no tiene versionado semántico ni releases
etiquetados, así que las entradas se agrupan por la tarea que las originó
en vez de por número de versión — formato inspirado en
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/), con las
categorías `Added` / `Changed` / `Fixed` dentro de cada una.

No se le asignó una fecha calendario a cada entrada porque este archivo
se compiló retrospectivamente a partir de una única conversación de
trabajo, y no hay certeza de en qué día calendario ocurrió cada tanda —
el orden de las secciones sí es fiel al orden real en que se hizo el
trabajo.

---

## Fallback de Postgres: bug de facetas + cache de corta duración

Motivado por un ítem pendiente ("cachear los conteos de facetas del
fallback de Postgres"). Al revisar `contar_facetas_hecho_judicial`/
`contar_facetas_declaracion` de cerca antes de cachearlas, apareció un
bug real de tres partes — se corrigió antes de cachear, para no cachear
resultados ya incorrectos.

### Fixed
- **Bug de auto-filtrado de facetas** (`db.py`): el propio docstring de
  las funciones decía que cada faceta debía excluir su propio filtro
  pero aplicar los demás ("estilo Letterboxd"). En la práctica:
  - `categorias_delito_ids`/`estados_judiciales` (y `tipos` en
    declaraciones) ni se aceptaban como parámetro — elegir una categoría
    no tenía ningún efecto en ningún conteo de faceta mostrado.
  - Las tres queries de conteo de hecho_judicial compartían un único
    `WHERE`, que incluía el filtro de provincia incluso al calcular la
    propia faceta de provincias — filtrar por Mendoza colapsaba el
    selector de provincias a mostrar solo Mendoza.
  - `total_aproximado` salía de sumar la faceta de categorías (que a
    propósito excluye su propio filtro), inflando el total en cuanto
    había un filtro de categoría activo.
  - Corregido: cada faceta arma su propio `WHERE`
    (`_construir_where(excluir=...)`), y `total` sale de un `COUNT(*)`
    aparte con todos los filtros aplicados.
- **Clientes de Redis atados a un event loop cerrado** (`rate_limit.py` y
  `facet_cache.py`): un cliente de `redis.asyncio` se ata al primer event
  loop que lo usa de verdad — con varios archivos de test usando
  `TestClient(app)` en secuencia (cada uno con su propio event loop de
  pytest-asyncio), `cerrar_cliente()` de un test posterior podía intentar
  cerrar un cliente creado en el event loop de un test anterior, ya
  cerrado (`RuntimeError: Event loop is closed`). Corregido en ambos
  módulos: `cerrar_cliente()` ahora ignora ese error puntual. No afectaba
  producción (ahí hay un solo event loop persistente durante toda la vida
  del proceso).

### Added
- `facet_cache.py`: cache de corta duración (TTL 5s por defecto) para los
  conteos de facetas — Redis si `REDIS_URL` está configurada (compartido
  entre workers/réplicas), cache en memoria del proceso si no. Fail-open
  ante errores de Redis.
- `tests/test_facet_cache.py`: lectura de env var y comportamiento del
  cache en memoria (hit, expiración por TTL, claves distintas no se
  pisan).
- `tests/test_facet_cache_e2e.py`: contra Redis real, que el valor quede
  guardado de verdad con el TTL pedido.
- `tests/test_facetas_fallback.py`: regresión del bug de auto-filtrado
  contra Postgres real.

### Changed
- `db.py`: `contar_facetas_hecho_judicial`/`contar_facetas_declaracion`
  ahora aceptan los filtros que antes faltaban, devuelven dicts planos en
  vez de `asyncpg.Record` (para ser JSON-serializables), e incluyen
  `total` en el resultado.
- `queries.py`: pasa el conjunto completo de filtros a las funciones de
  conteo, y envuelve la llamada con `facet_cache.obtener_o_calcular`.
- `app.py`: cierra el cliente de `facet_cache` en el shutdown.

---

## Tests de carga/concurrencia para calibrar el pool de asyncpg

Motivado por el último ítem pendiente de la lista original
("`min_size`/`max_size` del pool según el tráfico esperado"). Encontró un
bug de concurrencia real en el camino, no solo datos de calibración.

### Fixed
- **Condición de carrera en `db.get_pool()`**: sin lock, varios requests
  concurrentes contra un pool recién frío (`_pool is None`) podían
  disparar cada uno su propio `asyncpg.create_pool(...)` antes de que el
  primero terminara de asignarse a la variable global — los "perdedores"
  de esa carrera quedaban huérfanos, con sus conexiones abiertas para
  siempre. Confirmado de verdad: 20 llamadas concurrentes a `get_pool()`
  sin el lock creaban 20 pools distintos; con el lock, 1. Se manifestaba
  en la práctica como `TooManyConnectionsError: sorry, too many clients
  already` tras suficientes ráfagas. Corregido con double-checked
  locking.
- **`asyncio.Lock()` a nivel de módulo atado a un event loop cerrado**:
  mismo síntoma que el bug de los clientes de Redis de la sección
  anterior, pero encontrado primero acá — corregido con inicialización
  perezosa del lock, reseteado junto con el pool en `close_pool()`.

### Added
- `scripts/test_carga_pool.py`: herramienta de calibración manual — modo
  `raw` (solo `pool.acquire()`, aísla el comportamiento puro del pool),
  modo `graphql` (requests reales in-process vía ASGI, con una query
  `simple` de 1 conexión y una `ancha` de 6, el peor caso de fan-out de
  una ficha completa), y `--barrido` para comparar varias combinaciones
  de tamaño de pool × concurrencia en una sola corrida. Mide por
  separado el "arranque en frío" (primera ráfaga contra un pool recién
  creado) del régimen estable, porque son costos de naturaleza distinta.
- `tests/test_db_pool.py`: test de regresión puntual para la condición de
  carrera.
- `tests/test_pool_concurrencia.py`: smoke test rápido para CI (una
  ráfaga concurrente contra la query más ancha no rompe nada) — distinto
  del script de calibración, que es más lento y no tiene sentido gatear
  en cada push.

### Changed
- `db.py`: `min_size`/`max_size` del pool ahora configurables por
  `DB_POOL_MIN_SIZE`/`DB_POOL_MAX_SIZE` (mismos valores 2/10 como
  default) — permite calibrar en producción sin redeploy.

### Hallazgos empíricos (sandbox de desarrollo, no representativos de
producción en magnitud absoluta, pero el patrón relativo sí es real)
- Cero errores en las nueve combinaciones del barrido — asyncpg nunca
  falla un request por pool agotado, solo lo encola.
- Arranque en frío: ~400ms para abrir 20 conexiones físicas nuevas de una
  vez, contra ~4ms para la misma ráfaga con el pool ya caliente — dos
  órdenes de magnitud, no ruido de medición.
- La query `ancha` (6 conexiones por request) tiene sistemáticamente más
  cola (p99) que la `simple` con la misma concurrencia.
- Subir `max_size` de 10 a 20 no mejoró la latencia en régimen estable en
  el rango de carga probado — el costo dominante parece ser la cantidad
  de round-trips por request, no la escasez de lugares en el pool.

---

## Docker-compose para CI + tests end-to-end

Motivado por un ítem pendiente desde la integración de Meilisearch
("si más adelante se arma un docker-compose para CI, ahí es donde sumar
las pruebas end-to-end"). Cumple esa promesa: mueve a la suite automática
tres pruebas que hasta entonces se venían validando a mano en cada sesión
de trabajo.

### Added
- `docker-compose.yml`: Postgres 16 + Meilisearch v1.40.0 + Redis 7, cada
  uno con healthcheck, sin volúmenes nombrados (infraestructura de test,
  no de producción — cada `up` arranca limpio).
- `.github/workflows/tests.yml`: levanta el compose y corre `pytest
  tests/ -v` en cada push/PR.
- `tests/conftest.py`: fixture factory `api_key_temporal` — crea filas de
  `api_key` con COMMIT real (no la transacción rollback-based de la
  fixture `con`), necesaria para los tests que pegan por HTTP real contra
  el pool de conexiones propio de la app.
- `tests/test_search_engine_e2e.py`: indexar, buscar por texto, filtrar
  por faceta (incluido el array `organizaciones_ids`), forma de
  `facetDistribution`, que lo no-publicado nunca aparezca — contra
  Meilisearch real.
- `tests/test_rate_limit_e2e.py`: bloqueo exacto al superar el límite,
  contadores independientes por API key, headers en `200` y `429` —
  contra Redis real, vía `TestClient` sobre la app real.
- `tests/test_extensions_e2e.py`: la misma query anidada aceptada o
  rechazada según el `limite_profundidad_query` de la API key usada —
  contra el schema público real.

### Fixed
- Test de Meilisearch flaky por timing: la tarea de indexación real tardó
  ~0.85s en el sandbox de desarrollo, más de lo que alcanzaba un `sleep`
  fijo de 0.5s — reemplazado por polling con timeout.
- `DeprecationWarning` real de redis-py: `close()` → `aclose()`.

### Nota de validación
El `docker-compose.yml` en sí no se corrió contra un Docker real: el
entorno donde se armó esto no tenía Docker disponible. Se validó su
sintaxis/estructura YAML, y los tests `*_e2e.py` que va a alimentar se
corrieron y pasan de verdad — pero contra Postgres/Meilisearch/Redis
levantados directamente, no a través del compose. Sigue pendiente
confirmar `docker compose up -d --wait` contra Docker real.

---

## Límite de profundidad de query por API key

Motivado por un gap encontrado al implementar rate limiting: la tabla
`api_key` ya guardaba un `limite_profundidad_query` por fila, pero
`schema_publico` aplicaba un `QueryDepthLimiter(max_depth=10)` fijo para
todas las keys por igual.

### Added
- `extensions.py`: `LimiteProfundidadPorApiKey`, una extensión de
  Strawberry que lee `api_key.limite_profundidad_query` en el hook
  `on_operation()` (una vez por request, con el contexto de GraphQL ya
  disponible) en vez de tener el límite cerrado al armar el schema.
  Reusa `strawberry.extensions.query_depth_limiter.create_validator` — la
  misma lógica de conteo de profundidad que ya usaba `QueryDepthLimiter`
  — en vez de reimplementarla. La lógica de selección del límite
  (`_resolver_limite`) está separada del hook para poder testearla sin
  levantar Strawberry entero.
- `tests/test_extensions.py`: `_resolver_limite` (default, límite en
  cero/negativo, contexto ausente).

### Changed
- `schema.py`: reemplaza el `QueryDepthLimiter(max_depth=10)` fijo por
  `LimiteProfundidadPorApiKey`.

### Validado
Antes de escribir código, se confirmó leyendo el código fuente real de
Strawberry que las extensiones se instancian de cero por request y que
`execution_context.context` está disponible antes de `on_operation()`.
Después, misma query anidada contra el schema público real montado en
FastAPI, con dos API keys de límite distinto (10 y 2): la de límite 10
la dejó pasar, la de límite 2 la rechazó con `"exceeds maximum operation
depth of 2"` — confirmando que el número en el error es el de esa key
puntual, no un valor fijo global.

---

## Rate limiting por API key

Motivado por un ítem pendiente desde la integración de Meilisearch: el
campo `rate_limit_por_minuto` ya se guardaba por fila en `api_key`, pero
nada lo hacía cumplir.

### Added
- `rate_limit.py`: rate limiting por API key vía Redis (`INCR` +
  `EXPIRE`, ventana fija de 60s alineada al reloj). Sin `REDIS_URL`,
  queda deshabilitado (todo pasa, con un warning de log). Si Redis está
  configurado pero no responde en el momento de un request puntual:
  fail-open (se deja pasar, con `logger.error`) en vez de tumbar la API
  pública por la caída de un sistema secundario.
- Headers `X-RateLimit-Limit`/`X-RateLimit-Remaining`/`X-RateLimit-Reset`
  en cada response de la superficie pública, más `Retry-After` en el
  `429`.
- `tests/test_rate_limit.py`: lectura de env var, comportamiento
  deshabilitado.

### Changed
- `context.py`: `contexto_publico` ahora verifica el rate limit antes de
  dejar pasar el request hacia GraphQL.
- `app.py`: cierra el cliente de Redis en el shutdown.
- `requirements.txt`: agregado `redis>=5.0`.

### Fixed
- **Headers de rate limit no llegaban al cliente en las respuestas
  `429`**: mutar `response.headers` en la dependencia de FastAPI funciona
  para el camino exitoso (`200`) porque FastAPI comparte esa instancia de
  `Response` entre dependencias — pero cuando se levanta `HTTPException`,
  Starlette arma la respuesta de error **desde cero**
  (`fastapi.exception_handlers.http_exception_handler`) y no la mezcla
  con ese `Response` temporal. Detectado probando la integración real de
  punta a punta: los headers aparecían en los `200` pero desaparecían en
  los `429`, justo donde más importan (`Retry-After` le dice al cliente
  cuánto esperar). Corregido pasándolos explícitos vía
  `HTTPException(..., headers={...})`.

---

## Reindexado masivo inicial hacia Meilisearch

Primer ítem pendiente resuelto tras la integración de Meilisearch: la
sincronización por-escritura (`mutations.py`) solo cubre contenido creado
o editado después de que Meilisearch ya está configurado — hacía falta
un camino aparte para el primer deploy o para reconstruir el índice.

### Added
- `scripts/reindexar_meilisearch.py`: recorre todo lo publicado en
  Postgres paginado por cursor de id, e indexa cada tanda en Meilisearch
  con un solo POST (no uno por documento). Flags `--solo
  hechos|declaraciones|todos`, `--lote N`, `--sin-configurar-indices`.
  Idempotente (Meilisearch upsertea por `id`) — correrlo dos veces, o
  interrumpirlo y volver a correrlo, no duplica nada. Reusa los mismos
  constructores de documento que la sincronización por-escritura
  (`search_engine._documento_hecho_judicial`/`_documento_declaracion`),
  así el documento que arma el script es idéntico al de una escritura
  real.
- `db.py`: `contar_hechos_judiciales_publicados`,
  `contar_declaraciones_publicadas`, `fetch_lote_vista_busqueda_hechos_judiciales`,
  `fetch_lote_vista_busqueda_declaraciones` — mismas columnas que las
  vistas denormalizadas ya existentes, pero trayendo muchas filas por vez
  en vez de una, paginado por cursor.
- `search_engine.py`: `indexar_lote` — bulk POST de varios documentos en
  una sola llamada.

### Validado
Contra Postgres + Meilisearch reales: indexa solo lo `publicado` (un
hecho `pendiente_aprobacion` de prueba quedó afuera correctamente), la
paginación por cursor funciona con tandas chicas forzando múltiples
vueltas, `--solo` filtra bien, correrlo dos veces seguidas no duplica
documentos, y falla limpio con código de salida 1 si `MEILISEARCH_URL`
no está configurada.
