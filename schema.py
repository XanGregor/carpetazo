"""
Los dos schemas de GraphQL: interno (con mutaciones, para la web/apps
propias) y público (solo lectura, para terceros vía API key).

Límites de la superficie pública, para que nadie pueda tumbar el servidor
con una query anidada gigante — punto de partida razonable, ajustar según
lo que se observe en producción:
  - profundidad máxima de anidamiento: por API key (ver extensions.py;
    lee api_key.limite_profundidad_query en cada request, con 10 como
    fallback si no hay api_key en el contexto)
  - máximo 15 alias por query (evita "empaquetar" cientos de queries en
    una) — fijo, no hay un límite de alias por-key en el esquema de datos
  - máximo 1500 tokens por query — mismo motivo, fijo
"""
import strawberry
from strawberry.extensions import MaxAliasesLimiter, MaxTokensLimiter

from .extensions import LimiteProfundidadPorApiKey
from .mutations import Mutation
from .queries import Query

schema_interno = strawberry.Schema(query=Query, mutation=Mutation)

schema_publico = strawberry.Schema(
    query=Query,
    extensions=[
        lambda: LimiteProfundidadPorApiKey(),
        lambda: MaxAliasesLimiter(max_alias_count=15),
        lambda: MaxTokensLimiter(max_token_count=1500),
    ],
)
