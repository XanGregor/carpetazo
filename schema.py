"""
Los dos schemas de GraphQL: interno (con mutaciones, para la web/apps
propias) y público (solo lectura, para terceros vía API key).

Límites de la superficie pública, para que nadie pueda tumbar el servidor
con una query anidada gigante — punto de partida razonable, ajustar según
lo que se observe en producción:
  - profundidad máxima de 10 niveles de anidamiento
  - máximo 15 alias por query (evita "empaquetar" cientos de queries en una)
  - máximo 1500 tokens por query
"""
import strawberry
from strawberry.extensions import MaxAliasesLimiter, MaxTokensLimiter, QueryDepthLimiter

from .mutations import Mutation
from .queries import Query

schema_interno = strawberry.Schema(query=Query, mutation=Mutation)

schema_publico = strawberry.Schema(
    query=Query,
    extensions=[
        lambda: QueryDepthLimiter(max_depth=10),
        lambda: MaxAliasesLimiter(max_alias_count=15),
        lambda: MaxTokensLimiter(max_token_count=1500),
    ],
)
