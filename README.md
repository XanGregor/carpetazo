# API GraphQL — Archivo de Corrupción

Implementación en Strawberry GraphQL + FastAPI + asyncpg, sobre el esquema
definido en `archivo_corrupcion_schema.sql`.

## Estructura del paquete

| Archivo | Responsabilidad |
|---|---|
| `enums.py` | Enumerados GraphQL, espejo de los ENUM de Postgres |
| `types.py` | Tipos GraphQL (entidades) con resolvers de relaciones |
| `inputs.py` | Inputs de filtro/paginación/mutación y tipos de resultado paginado |
| `mappers.py` | asyncpg.Record → tipos de Strawberry (compartido) |
| `db.py` | Toda la SQL parametrizada: lectura, búsqueda faceteada, escritura |
| `search_engine.py` | Sincronización con Meilisearch (indexar/borrar/buscar); no-op si no está configurado |
| `rate_limit.py` | Rate limiting por API key (Redis); no-op si no está configurado |
| `extensions.py` | Extensión de Strawberry: límite de profundidad de query por API key |
| `dataloaders.py` | Un DataLoader por relación, para evitar N+1 |
| `auth.py` | JWT (equipo interno), hashing de contraseñas, API keys (terceros) |
| `permissions.py` | Clases de permiso por rol |
| `context.py` | context_getter de FastAPI para ambos routers |
| `queries.py` | Query root (fichas + búsqueda faceteada, con o sin Meilisearch) |
| `mutations.py` | Mutation root (solo en el schema interno) |
| `schema.py` | Construye `schema_interno` y `schema_publico` |
| `app.py` | FastAPI: monta ambos routers y el ciclo de vida de la pool |

Fuera del paquete `graphql_api/`, en `scripts/`:

| Archivo | Responsabilidad |
|---|---|
| `reindexar_meilisearch.py` | Reindexado masivo inicial (primer deploy, o reconstruir el índice) |

También en la raíz del repo:

| Archivo | Responsabilidad |
|---|---|
| `docker-compose.yml` | Postgres + Meilisearch + Redis para desarrollo local y CI (ver sección "Tests end-to-end / CI") |
| `.github/workflows/tests.yml` | Levanta el compose y corre `pytest tests/` en cada push/PR |

Todo el paquete fue validado con **tests de integración reales contra
PostgreSQL** (no solo import checks): 46 tests que corren el schema SQL
completo, ejecutan mutations y queries de GraphQL de punta a punta,
verifican los permisos por rol, y prueban la construcción de documentos/
filtros de Meilisearch — más otros 12 tests end-to-end contra Meilisearch,
Redis y el schema público real, corriendo con `docker-compose.yml` (ver
"Tests end-to-end / CI" más abajo). Ese proceso encontró y corrigió varios
bugs reales antes de que llegaran a producción — ver "Qué encontraron los
tests" más abajo.

## Tests

```bash
# Requiere PostgreSQL corriendo localmente con una base de prueba ya
# inicializada con archivo_corrupcion_schema.sql:
createdb archivo_corrupcion_test
psql -d archivo_corrupcion_test -f archivo_corrupcion_schema.sql

pip install pytest pytest-asyncio
DATABASE_URL="postgresql://usuario:password@localhost/archivo_corrupcion_test" \
JWT_SECRET="cualquier-valor-para-tests" \
python -m pytest tests/ -v
```

Los tests están en `tests/`, organizados por capa:

| Archivo | Qué cubre |
|---|---|
| `test_schema_sql.py` | Triggers (código automático, `actualizado_en`), constraints (fuente exige un solo padre, `afiliacion` no permite `fecha_fin` < `fecha_inicio`), validaciones polimórficas (`hecho_relacion`/`hecho_persona` contra hechos inexistentes), datos semilla |
| `test_db_queries.py` | La lógica de filtrado (OR dentro de una faceta, AND entre facetas), paginación por cursor, conteo de facetas, y el caso de uso central de `afiliacion`: reconstruir en qué partido estaba alguien en la fecha de un hecho |
| `test_graphql_flow.py` | Flujo de moderación de punta a punta (colaborador propone → admin aprueba → aparece en la búsqueda pública, con su registro en el audit log), permisos por rol, y el caso real de la legisladora (`hecho_relacion` cruzando `declaracion` → `hecho_judicial`) |
| `test_schema_publico.py` | Que el schema público no tenga `Mutation`, y que el límite de profundidad de query realmente rechace una consulta anidada de más de 10 niveles (no solo que esté declarado) |
| `test_search_engine.py` | Construcción de documentos (con nombres de relaciones ya resueltos, arrays vacíos en vez de `None`) y de los filtros de Meilisearch (OR interno, AND entre facetas) — sin necesitar una instancia de Meilisearch corriendo |
| `test_rate_limit.py` | Lectura de la variable de entorno y comportamiento con rate limiting deshabilitado — sin necesitar una instancia de Redis corriendo |
| `test_extensions.py` | Lógica de selección del límite de profundidad por API key (default, límite en cero/negativo, contexto ausente) — sin necesitar levantar un execution_context real de Strawberry |
| `test_search_engine_e2e.py` | **End-to-end contra Meilisearch real**: indexar, buscar por texto, filtrar por faceta (incluido el array `organizaciones_ids`), forma de `facetDistribution`, que lo no-publicado nunca aparezca, y borrado — se salta si no hay `MEILISEARCH_URL` |
| `test_rate_limit_e2e.py` | **End-to-end contra Redis real** vía `TestClient` sobre la app real: bloqueo exacto al superar el límite, contadores independientes por API key, y que los headers `X-RateLimit-*`/`Retry-After` lleguen en el `200` **y** en el `429` — se salta si no hay `REDIS_URL` |
| `test_extensions_e2e.py` | **End-to-end contra el schema público real**: la misma query anidada rechazada o aceptada según el `limite_profundidad_query` de la API key usada |

Cada test corre dentro de una transacción que se revierte al final
(`tests/conftest.py`), así que no hace falta limpiar la base entre tests
ni mantener fixtures de datos permanentes — con la excepción de los tests
`*_e2e.py`, que necesitan datos realmente commiteados para que la app
(con su propio pool de conexiones) los vea; ver la sección siguiente.

### Tests end-to-end / CI

Los archivos `test_search_engine_e2e.py`, `test_rate_limit_e2e.py` y
`test_extensions_e2e.py` corren contra instancias reales de Meilisearch/
Redis (además de Postgres, que ya hace falta para toda la suite) — antes
de tener `docker-compose.yml` estas pruebas se hacían a mano en cada
sesión de trabajo; ahora están automatizadas.

```bash
docker compose up -d --wait   # Postgres + Meilisearch + Redis
psql -h localhost -U postgres -d archivo_corrupcion_test -f archivo_corrupcion_schema.sql  # solo si el compose no lo cargó solo, ver nota abajo

DATABASE_URL="postgresql://postgres:postgres@localhost:5432/archivo_corrupcion_test" \
JWT_SECRET="cualquier-valor-para-tests" \
MEILISEARCH_URL="http://localhost:7700" \
MEILISEARCH_API_KEY="clave-de-desarrollo" \
REDIS_URL="redis://localhost:6379" \
python -m pytest tests/ -v

docker compose down -v
```

- `docker-compose.yml` monta `archivo_corrupcion_schema.sql` como script
  de inicialización de Postgres (`docker-entrypoint-initdb.d/`) — se
  carga solo la primera vez que el contenedor arranca con un data
  directory vacío. El `psql` de arriba es solo para el caso de correr
  Postgres por fuera del compose.
- Los tests `*_e2e.py` se saltean solos (`SKIPPED`, no `FAILED`) si
  `MEILISEARCH_URL`/`REDIS_URL` no están seteadas — no todos los entornos
  donde corre la suite van a tener los tres servicios levantados.
- Necesitan una API key real commiteada en la base (no alcanza con la
  transacción de la fixture `con`, que se revierte): la app real usa su
  propio pool de conexiones (`db.get_pool()`), que no ve nada de lo que
  esté sin commitear en la transacción del test. `conftest.py` trae un
  fixture factory para esto — `api_key_temporal` — que inserta con
  COMMIT real y borra a mano al terminar el test.
- `.github/workflows/tests.yml` corre exactamente este mismo compose en
  cada push/PR.

### Qué encontraron los tests (y ya está corregido en este paquete)

1. **Bug real de concurrencia** (el más importante): el diseño original
   compartía una sola conexión de asyncpg por request entre todos los
   DataLoaders y resolvers. GraphQL resuelve campos hermanos en
   paralelo — al pedir `categoriaDelito`, `provincia` y `fuentes` de un
   mismo hecho en una sola query, las tres relaciones se resuelven
   concurrentemente, y una sola conexión de asyncpg no soporta dos
   queries al mismo tiempo. Esto tiraba `InterfaceError: cannot perform
   operation: another operation is in progress` en cualquier ficha con
   más de una relación — es decir, en la inmensa mayoría de las
   consultas reales. Se corrigió pasando de "una conexión por request"
   a "un pool por request, cada resolver saca su propia conexión"
   (`context.py`, `dataloaders.py`, `queries.py`, `mutations.py`).
2. **`now()` queda congelado dentro de una transacción**: el trigger de
   `actualizado_en` usaba `now()`, que en Postgres devuelve el mismo
   valor durante toda la transacción — si una transacción hace varios
   `UPDATE`, todos quedan con idéntico timestamp. Se cambió a
   `clock_timestamp()`, que sí refleja el instante real de cada
   statement (corregido tanto acá como en `archivo_corrupcion_schema.sql`).
3. Un warning de deprecación real de Strawberry (pasar instancias de
   extensión en vez de factories) — corregido en `schema.py`.

## Instalación

```bash
pip install -r requirements.txt
```

## Variables de entorno

```bash
export DATABASE_URL="postgresql://usuario:password@localhost:5432/archivo_corrupcion"
export JWT_SECRET="una-clave-larga-y-aleatoria"   # nunca el valor default en producción
```

Opcionales, cada una con su propio fallback si no se setea (ver las
secciones "Meilisearch" y "Rate limiting" más abajo): `MEILISEARCH_URL`,
`MEILISEARCH_API_KEY`, `REDIS_URL`.

## Correr en desarrollo

```bash
uvicorn graphql_api.app:app --reload
```

- Interno (con mutaciones): `http://localhost:8000/graphql`
- Público (solo lectura, requiere header `X-API-Key`): `http://localhost:8000/api/publico/graphql`

Ambos exponen GraphiQL en el navegador para explorar el schema interactivamente.

## Ejemplos

### Login (obtener JWT)

```graphql
mutation {
  iniciarSesion(input: { email: "admin@ejemplo.com", password: "..." }) {
    token
    rol
  }
}
```

Después, mandar el token en cada request al endpoint interno:
`Authorization: Bearer <token>`

### Búsqueda faceteada (estilo Letterboxd)

```graphql
query {
  buscarHechosJudiciales(
    filtro: {
      categoriasDelitoIds: ["3", "7"]
      provinciasIds: ["12"]
      fechaDesde: "2020-01-01"
    }
    paginacion: { limite: 20 }
  ) {
    items {
      codigo
      titulo
      estadoJudicial
      categoriaDelito { nombre }
      fuentes { nivel url }
    }
    hayMas
    cursorSiguiente
    totalAproximado
    facetas {
      categoriasDelito { etiqueta cantidad }
      provincias { etiqueta cantidad }
    }
  }
}
```

### Ficha con hechos relacionados (el caso de la legisladora)

```graphql
query {
  declaracion(codigo: "DE-000045") {
    titulo
    relaciones {
      tipoRelacion
      descripcion
      hecho {
        ... on HechoJudicial { codigo titulo estadoJudicial }
        ... on Declaracion { codigo titulo }
      }
    }
  }
}
```

### Registro para API key pública

```graphql
mutation {
  solicitarApiKey(input: { nombre: "Ana Editora", email: "ana@medio.com", usoPrevisto: "Seguimiento de causas de lavado" }) {
    key    # se muestra UNA sola vez — avisarle al usuario que la guarde
  }
}
```

## Meilisearch (búsqueda faceteada)

`buscar_hechos_judiciales` y `buscar_declaraciones` usan Meilisearch
cuando está configurado (variable `MEILISEARCH_URL`), y caen solas al
fallback de Postgres (`ILIKE` + `GROUP BY`) si no — el proyecto anda en
desarrollo local sin tener que levantar el motor.

### Cómo funciona

- Cada mutation que crea/edita/aprueba/rechaza un hecho o declaración
  sincroniza el documento correspondiente **después** de que la
  transacción de Postgres ya cerró (nunca adentro) — así un fallo de
  Meilisearch nunca revierte un cambio ya confirmado en la base. Si la
  sincronización falla, queda logueada (`logger.exception`) pero la
  mutation igual responde con éxito: la búsqueda es eventualmente
  consistente, no una condición para poder guardar contenido.
- Solo se indexan hechos con `estado_publicacion = "publicado"` — un
  hecho que pasa a pendiente/rechazado se **saca** del índice
  automáticamente.
- Postgres sigue siendo la fuente de verdad: Meilisearch decide QUÉ ids
  matchean una búsqueda + los conteos por faceta (`facetDistribution`,
  nativo del motor — es exactamente el conteo en vivo tipo Letterboxd que
  se buscaba, sin calcularlo a mano); los objetos que se devuelven al
  cliente GraphQL se hidratan desde Postgres vía los DataLoaders
  existentes, así un documento desactualizado en el índice nunca termina
  mostrando datos viejos.
- El cliente es REST directo con `httpx.AsyncClient` (`search_engine.py`),
  no el paquete oficial `meilisearch` — ese paquete es sync-only y
  bloquearía el event loop de FastAPI/Strawberry.

### Variables de entorno

```bash
export MEILISEARCH_URL="http://localhost:7700"
export MEILISEARCH_API_KEY="tu-clave-de-meilisearch"   # opcional en dev, obligatoria en producción
```

### Correr Meilisearch en desarrollo

```bash
curl -L https://install.meilisearch.com | sh
MEILI_MASTER_KEY="clave-de-desarrollo" ./meilisearch
```

Al arrancar, `app.py` llama a `search_engine.configurar_indices()`
(define qué campos son buscables/filtrables/ordenables en cada índice) —
no hace falta correr nada a mano aparte de tener el motor levantado.

### Reindexado masivo (`scripts/reindexar_meilisearch.py`)

La sincronización de `mutations.py` solo cubre contenido que se crea o
edita **después** de que Meilisearch ya está configurado. Hace falta un
reindexado masivo aparte en dos casos: el primer deploy con Meilisearch
(ya hay contenido publicado de antes en Postgres, y el índice arranca
vacío), o reconstruir el índice desde cero (se borró por accidente, se
cambió `configurar_indices()` de forma incompatible, o se migra a una
instancia nueva).

```bash
# Desde la raíz del repo, con las mismas variables de entorno que la API
# (DATABASE_URL, MEILISEARCH_URL, y MEILISEARCH_API_KEY si aplica):
python -m scripts.reindexar_meilisearch

# Reindexar solo un tipo de contenido:
python -m scripts.reindexar_meilisearch --solo hechos
python -m scripts.reindexar_meilisearch --solo declaraciones

# Tamaño de tanda hacia Postgres/Meilisearch (default 500):
python -m scripts.reindexar_meilisearch --lote 200

# Si ya se corrió antes y el settings de Meilisearch no cambió, se puede
# saltear el PATCH de configuración de índices:
python -m scripts.reindexar_meilisearch --sin-configurar-indices
```

Reusa los mismos constructores de documento que la sincronización normal
(`search_engine._documento_hecho_judicial` / `_documento_declaracion`),
así el documento que arma el script es idéntico al que arma una escritura
en producción — no hay dos caminos de armado de documento que puedan
desincronizarse entre sí. Recorre Postgres paginado por cursor de id
(`db.fetch_lote_vista_busqueda_*`) para no cargar todo el archivo en
memoria de una, y manda cada tanda a Meilisearch en un solo POST
(`search_engine.indexar_lote`) en vez de un request por documento.

Es idempotente: Meilisearch upsertea por `id`, así que correrlo dos veces
— o interrumpirlo a mitad de camino y volver a correrlo desde el
principio — no duplica nada. Validado de punta a punta contra una
instancia real de Postgres + Meilisearch: indexa solo lo publicado
(un hecho `pendiente_aprobacion` de prueba quedó afuera correctamente),
la paginación por cursor funciona con tandas chicas forzando múltiples
vueltas, `--solo` filtra bien, y correrlo dos veces seguidas no duplica
documentos.

### Qué se validó

`test_search_engine.py` cubre la construcción de documentos y filtros sin
necesitar el motor corriendo. La sincronización end-to-end en sí —indexar,
buscar por texto, filtrar por faceta (incluyendo el filtro por array
`organizaciones_ids`, que en Meilisearch funciona como "el array contiene
este valor"), que `facetDistribution` tenga la forma que `queries.py`
espera, y que lo no-publicado nunca aparezca— **ya está automatizada** en
`test_search_engine_e2e.py`, corriendo contra una instancia real de
Meilisearch 1.x (ver "Tests end-to-end / CI" más abajo). Antes de tener
`docker-compose.yml` esto se validaba a mano en cada sesión de trabajo;
ahora corre solo en cada push.

### Filtros de texto libre no cubiertos todavía

`personas_nombres` y `organizaciones_nombres` se indexan como buscables
para que buscar el nombre de una persona/organización encuentre los
hechos donde aparece, pero no hay un filtro dedicado por persona (solo
por organización) — si hace falta "todos los hechos de tal persona" como
filtro explícito (no solo búsqueda de texto), agregar `personas_ids`
como filterableAttribute siguiendo el mismo patrón que
`organizaciones_ids`.

## Rate limiting (API pública)

`rate_limit.py` hace cumplir el `rate_limit_por_minuto` que ya se
guardaba por fila en `api_key` pero que hasta ahora nadie hacía cumplir.
Es la pieza que sostiene la decisión de registro automático sin
aprobación manual (`solicitarApiKey`): el control de abuso no pasa por
filtrar quién se registra, pasa por acá — si una key abusa, se revoca
puntualmente después de detectarlo, no se audita antes de emitirla.

### Cómo funciona

- Contador compartido en Redis (`INCR` + `EXPIRE`), ventana fija de 60s
  alineada al reloj — no al primer request de cada cliente, así todas
  las API keys comparten el mismo punto de corte de minuto. Se aplica
  **solo** en la superficie pública (`contexto_publico`, en
  `context.py`) — la interna usa JWT y no pasa por acá.
- Por qué Redis y no un contador en memoria del proceso: en cuanto
  FastAPI/uvicorn corre con más de un worker, cada worker tendría su
  propio contador y el límite real terminaría siendo
  `rate_limit_por_minuto × cantidad_de_workers`. Redis es el contador
  compartido entre workers.
- Cada response de la superficie pública trae los headers
  `X-RateLimit-Limit`, `X-RateLimit-Remaining` y `X-RateLimit-Reset`
  (segundos hasta que abre la próxima ventana). Al superar el límite, la
  respuesta es `429` con `Retry-After` además de los anteriores.
- Trade-off conocido y aceptado de la ventana fija (misma lógica que los
  límites fijos de profundidad/alias/tokens de `schema.py`): un cliente
  puede rozar ~2x el límite si concentra requests justo en el borde entre
  dos minutos consecutivos. Punto de partida razonable — pasar a sliding
  window si en producción se ve abuso real aprovechando ese borde.
- Sin Redis disponible: **fail-open**, no fail-closed. Sin `REDIS_URL`
  seteada, el rate limiting queda deshabilitado (se loguea un warning una
  sola vez al arrancar) — mismo patrón que Meilisearch, para no forzar a
  levantar Redis también solo para desarrollar localmente. Si `REDIS_URL`
  está seteada pero Redis no responde en el momento de un request
  puntual, también se deja pasar el request (con `logger.error`) en vez
  de tumbar la API pública entera por la caída de un sistema secundario —
  mismo criterio que ya se usa con fallos de Meilisearch. Si se prefiere
  el criterio inverso (negar el request si Redis no responde,
  priorizando el control de abuso por sobre la disponibilidad), está
  documentado en el docstring de `rate_limit.py` dónde invertirlo.

### Variables de entorno

```bash
export REDIS_URL="redis://localhost:6379"
```

### Un detalle no obvio de FastAPI que esto encontró

`contexto_publico` recibe un parámetro `response: Response` y en el
camino exitoso simplemente hace `response.headers.update(...)` — FastAPI
comparte esa misma instancia de `Response` entre todas las dependencias
de un request, así que los headers puestos ahí llegan de verdad a la
respuesta final aunque el dependency-getter termine antes de que
Strawberry arme el cuerpo de la respuesta.

**Pero eso deja de ser cierto en el camino de error.** Cuando se levanta
`HTTPException` (el caso 429), Starlette arma la respuesta de error
**desde cero** (`fastapi.exception_handlers.http_exception_handler`) y
no la mezcla con el `Response` temporal de la dependencia — mutar
`response.headers` ahí se pierde en silencio, sin ningún error que lo
avise. Se detectó probando la integración real de punta a punta (no solo
import checks): en la primera versión, `X-RateLimit-*` y `Retry-After`
aparecían correctamente en los `200` pero desaparecían en los `429`,
exactamente donde más importan (es el header que le dice al cliente
cuánto esperar antes de reintentar). La corrección fue pasarlos
explícitos vía `HTTPException(..., headers={...})`, que sí es tomado por
el handler. Quedó comentado en el código en el lugar exacto para que no
se repita el error si se toca ese bloque más adelante.

### Qué se validó

`test_rate_limit.py` cubre la lectura de la variable de entorno y el
comportamiento con Redis deshabilitado, sin necesitar una instancia
corriendo. El resto —que se bloquee exactamente al superar el límite y
no antes, que dos API keys tengan contadores independientes, y sobre
todo que los headers `X-RateLimit-*`/`Retry-After` lleguen de verdad al
cliente HTTP en el `200` **y** en el `429`— **ya está automatizado** en
`test_rate_limit_e2e.py`, corriendo contra un Redis real vía `TestClient`
sobre la app real (ver "Tests end-to-end / CI" más abajo). El modo
fail-open cuando Redis no responde en absoluto no está cubierto por un
test automático (habría que tumbar Redis a mitad de un test, que es más
lío que valor por ahora) — se validó a mano apuntando `REDIS_URL` a un
puerto sin nada escuchando.

## Límite de profundidad de query por API key

`schema_publico` tenía un `QueryDepthLimiter(max_depth=10)` fijo — aplicaba
igual para todas las API keys, aunque la tabla `api_key` ya guardaba un
`limite_profundidad_query` distinto por fila que nadie leía (quedó
anotado como pendiente al implementar rate limiting). `extensions.py`
resuelve esto sin tocar la lógica de conteo de profundidad de Strawberry.

### Cómo funciona

- `QueryDepthLimiter(max_depth=N)` cierra `N` en el momento de construir
  el validador — antes de que exista ningún request, así que no hay forma
  de que dependa de qué API key está pegando con un valor fijo.
- Se resolvió con una extensión propia (`LimiteProfundidadPorApiKey`) que,
  en vez de recibir `max_depth` fijo, lo lee en el hook `on_operation()` —
  que Strawberry corre una vez por request, ya con
  `execution_context.context` (el mismo dict que arma `contexto_publico`)
  disponible. Ahí arma el validador para *ese* request puntual, reusando
  `strawberry.extensions.query_depth_limiter.create_validator` (la misma
  lógica de conteo que usaba `QueryDepthLimiter`) en vez de reescribirla.
- La selección del límite (`_resolver_limite`) vive separada del hook
  para poder testearla sin levantar Strawberry entero: usa
  `api_key.limite_profundidad_query` si hay una API key en el contexto y
  es un número positivo; si no (sin api_key, o un valor en cero/negativo
  por misconfiguración), cae a `LIMITE_POR_DEFECTO = 10` — el mismo valor
  que tenía el límite fijo anterior, así nadie queda sin protección por
  un dato faltante o mal cargado.
- `MaxAliasesLimiter`/`MaxTokensLimiter` en `schema.py` se quedan con su
  valor fijo global — la tabla `api_key` no tiene columnas de alias/tokens
  por key, solo de profundidad, así que no hay nada por-key que leer ahí.

### Qué se validó

`test_extensions.py` cubre `_resolver_limite` (default, límite en
cero/negativo, contexto ausente o con forma rara) sin necesitar el stack
completo. Que el límite realmente se aplique —con el mensaje de error
mostrando el número correcto— **ya está automatizado** en
`test_extensions_e2e.py`: la misma query GraphQL anidada, con dos API
keys de límite distinto (10 y 2), contra el schema público real montado
en FastAPI (ver "Tests end-to-end / CI" más abajo). La de límite 10 la
deja pasar (`200`, sin errores); la de límite 2 la rechaza con
`"exceeds maximum operation depth of 2"` — confirmando que el número que
aparece en el error es el de esa key puntual, no un valor fijo global.

## Pendiente antes de producción

- **Tests de carga/concurrencia real**: los tests actuales prueban
  correctud funcional contra Postgres real; falta un test que abra
  muchas conexiones simultáneas contra un pool chico para calibrar
  `min_size`/`max_size` en `db.py` según el tráfico esperado.
- **Índice de conteo de facetas (fallback Postgres)**: `contar_facetas_*`
  hace varios `GROUP BY` por request; solo aplica cuando Meilisearch no
  está configurado, pero si ese fallback se usa en producción con
  volumen alto, conviene cachear estos conteos unos segundos.
- **`docker-compose.yml` en sí no se corrió contra un Docker real todavía**:
  se armó y se validó su YAML (sintaxis, estructura, los tres servicios
  con healthcheck), y los tests `*_e2e.py` que va a alimentar se
  corrieron y pasan de verdad — pero contra Postgres/Meilisearch/Redis
  levantados directamente (no contenerizados), no a través del compose
  en sí, porque el entorno donde se armó esto no tenía Docker disponible.
  Antes de confiar en el workflow de CI, conviene correr
  `docker compose up -d --wait` una vez a mano y confirmar que los tres
  healthchecks pasan y que `pytest tests/ -v` corre igual de verde apuntando
  a esos puertos.
