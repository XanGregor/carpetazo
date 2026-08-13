"""
Límite de profundidad de query POR API KEY — reemplaza el max_depth fijo
que tenía schema_publico (`QueryDepthLimiter(max_depth=10)` aplicaba
igual para todas las keys, aunque la tabla `api_key` ya guarda un
`limite_profundidad_query` distinto por fila que nadie leía — quedó
anotado como pendiente en el README tras implementar rate_limit.py).

Por qué no alcanza con pasarle un valor fijo a QueryDepthLimiter: ese
límite queda cerrado (baked in) en el momento de construir el validador,
antes de que exista ningún request — no hay forma de que dependa de qué
API key está pegando. Para que dependa de la key hace falta leer el
límite en el momento en que se resuelve CADA request, no al armar el
schema.

Cómo se logra, sin reimplementar el conteo de profundidad: Strawberry ya
expone `execution_context.context` (el mismo dict que arma
`contexto_publico` en context.py, con la `ApiKeyActual` adentro) dentro
del hook `on_operation()` de una extensión — confirmado leyendo el código
fuente real de la librería (Schema.execute_async / execute_sync asignan
`extension.execution_context = execution_context` ANTES de correr
`extensions_runner.operation()`, que es lo que dispara `on_operation`).
`on_operation` corre una vez por request, antes de que empiece la
validación real. Ahí se lee el límite de ESE request puntual y se arma el
validador correspondiente reusando
`strawberry.extensions.query_depth_limiter.create_validator` — la misma
lógica de conteo de profundidad que ya usaba QueryDepthLimiter — en vez
de reescribirla.

Nota de alcance: `api_key` solo trae `limite_profundidad_query` (no hay
un alias/token límite por key en la tabla) — MaxAliasesLimiter y
MaxTokensLimiter en schema.py se quedan con su valor fijo global, que es
lo que corresponde dado lo que hoy existe en el esquema de datos.
"""
from __future__ import annotations

from collections.abc import Iterator

from strawberry.extensions import SchemaExtension
from strawberry.extensions.query_depth_limiter import create_validator

LIMITE_POR_DEFECTO = 10


def _resolver_limite(contexto: object) -> int:
    """
    Determina qué límite de profundidad aplica para esta operación puntual,
    a partir del contexto de GraphQL (el mismo dict que arma
    contexto_publico en context.py). Separada de on_operation a propósito:
    así se puede testear la lógica de selección del límite (incluyendo los
    casos raros — sin api_key, límite en cero o negativo) sin tener que
    levantar un execution_context real de Strawberry — ver
    tests/test_extensions.py.

    Cae a LIMITE_POR_DEFECTO si no hay api_key en el contexto — no debería
    pasar en tráfico público real (contexto_publico siempre la deja puesta
    o corta el request antes con 401/429, ver context.py), pero es una red
    de seguridad razonable para invocaciones directas del schema fuera del
    router HTTP normal (ej: tests que llaman schema_publico.execute(...) a
    mano) — mejor un límite conservador por defecto que ningún límite.
    """
    api_key = contexto.get("api_key") if isinstance(contexto, dict) else None
    limite = getattr(api_key, "limite_profundidad_query", None) if api_key is not None else None
    if not limite or limite <= 0:
        return LIMITE_POR_DEFECTO
    return limite


class LimiteProfundidadPorApiKey(SchemaExtension):
    def on_operation(self) -> Iterator[None]:
        limite = _resolver_limite(self.execution_context.context)
        validador = create_validator(limite, should_ignore=None)
        self.execution_context.validation_rules = (
            self.execution_context.validation_rules + (validador,)
        )
        yield
