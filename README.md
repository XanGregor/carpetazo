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
| `dataloaders.py` | Un DataLoader por relación, para evitar N+1 |
| `auth.py` | JWT (equipo interno), hashing de contraseñas, API keys (terceros) |
| `permissions.py` | Clases de permiso por rol |
| `context.py` | context_getter de FastAPI para ambos routers |
| `queries.py` | Query root (fichas + búsqueda faceteada, con o sin Meilisearch) |
| `mutations.py` | Mutation root (solo en el schema interno) |
| `schema.py` | Construye `schema_interno` y `schema_publico` |
| `app.py` | FastAPI: monta ambos routers y el ciclo de vida de la pool |

Todo el paquete fue validado con **tests de integración reales contra
PostgreSQL** (no solo import checks): 46 tests que corren el schema SQL
completo, ejecutan mutations y queries de GraphQL de punta a punta,
verifican los permisos por rol, y prueban la construcción de documentos/
filtros de Meilisearch. La sincronización con Meilisearch en sí (indexar,
buscar, facetas, borrado) se validó aparte contra una instancia real — ver
la sección "Meilisearch" más abajo. Ese proceso encontró y corrigió dos
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

Cada test corre dentro de una transacción que se revierte al final
(`tests/conftest.py`), así que no hace falta limpiar la base entre tests
ni mantener fixtures de datos permanentes.

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

### Qué se validó

`test_search_engine.py` cubre la construcción de documentos y filtros
(sin necesitar el motor corriendo). La sincronización end-to-end en sí
—indexar, buscar por texto, filtrar por faceta (incluyendo el filtro por
array `organizaciones_ids`, que en Meilisearch funciona como "el array
contiene este valor"), que `facetDistribution` tenga la forma que
`queries.py` espera, que lo no-publicado nunca aparezca, y que borrar
funcione— se probó a mano contra una instancia real de Meilisearch
1.x, con resultados correctos en los cinco casos. No quedó en la suite
automática porque encadenar dos servicios (Postgres + Meilisearch) en
cada corrida de tests es infraestructura que no se justifica todavía
para este proyecto; si más adelante se arma un `docker-compose` para CI,
ese es el lugar natural para sumar esa prueba end-to-end.

### Filtros de texto libre no cubiertos todavía

`personas_nombres` y `organizaciones_nombres` se indexan como buscables
para que buscar el nombre de una persona/organización encuentre los
hechos donde aparece, pero no hay un filtro dedicado por persona (solo
por organización) — si hace falta "todos los hechos de tal persona" como
filtro explícito (no solo búsqueda de texto), agregar `personas_ids`
como filterableAttribute siguiendo el mismo patrón que
`organizaciones_ids`.

## Pendiente antes de producción

- **Tests de carga/concurrencia real**: los tests actuales prueban
  correctud funcional contra Postgres real; falta un test que abra
  muchas conexiones simultáneas contra un pool chico para calibrar
  `min_size`/`max_size` en `db.py` según el tráfico esperado.
- **Rate limiting real** por API key (acá se guarda `rate_limit_por_minuto`
  en la tabla, pero falta el middleware que lo haga cumplir — ej. con
  `slowapi` o un contador en Redis).
- **Reindexado masivo inicial**: `search_engine.py` sincroniza un
  documento por escritura, pero no hay un script que recorra todo lo ya
  publicado y lo indexe de una — hace falta para el primer deploy con
  Meilisearch, o para reconstruir el índice si hay que borrarlo.
- **Docker-compose para CI**: correr Postgres + Meilisearch juntos en la
  suite de tests (ver nota en la sección de Meilisearch).
- **Índice de conteo de facetas (fallback Postgres)**: `contar_facetas_*`
  hace varios `GROUP BY` por request; solo aplica cuando Meilisearch no
  está configurado, pero si ese fallback se usa en producción con
  volumen alto, conviene cachear estos conteos unos segundos.
