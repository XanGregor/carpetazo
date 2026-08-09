"""
Enumerados GraphQL que reflejan los tipos ENUM de PostgreSQL definidos en
archivo_corrupcion_schema.sql. Si se agrega un valor al ENUM de la base de
datos, hay que agregarlo acá también — no se sincronizan solos.
"""
from enum import Enum

import strawberry


@strawberry.enum
class RolUsuario(Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    COLABORADOR = "colaborador"


@strawberry.enum
class EstadoPublicacion(Enum):
    BORRADOR = "borrador"
    PENDIENTE_APROBACION = "pendiente_aprobacion"
    PUBLICADO = "publicado"
    RECHAZADO = "rechazado"


@strawberry.enum
class EstadoJudicial(Enum):
    DENUNCIA = "denuncia"
    INVESTIGACION = "investigacion"
    PROCESADO = "procesado"
    JUICIO = "juicio"
    CONDENADO = "condenado"
    ABSUELTO = "absuelto"
    SOBRESEIDO = "sobreseido"


@strawberry.enum
class TipoOrganizacion(Enum):
    PARTIDO_POLITICO = "partido_politico"
    EMPRESA = "empresa"
    ONG = "ong"
    FUNDACION = "fundacion"
    ORGANISMO_PUBLICO = "organismo_publico"
    OTRO = "otro"


@strawberry.enum
class NivelFuente(Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"


@strawberry.enum
class TipoRelacionHecho(Enum):
    POSIBILITO = "posibilito"
    CONSECUENCIA_DE = "consecuencia_de"
    CONTEXTO_DE = "contexto_de"
    MISMO_PATRON = "mismo_patron"


@strawberry.enum
class TipoDeclaracion(Enum):
    VOTO_LEGISLATIVO = "voto_legislativo"
    PROYECTO_LEY = "proyecto_ley"
    DECLARACION_PUBLICA = "declaracion_publica"
    PUBLICACION_REDES = "publicacion_redes"


@strawberry.enum
class EstadoReporte(Enum):
    PENDIENTE = "pendiente"
    EN_REVISION = "en_revision"
    RESUELTO = "resuelto"
    DESCARTADO = "descartado"


@strawberry.enum
class TipoFinanciamiento(Enum):
    ESTATAL_NO_DECLARADO = "estatal_no_declarado"
    EXTRANJERO_NO_DECLARADO = "extranjero_no_declarado"
    PARTIDARIO = "partidario"
    FONDOS_ILICITOS = "fondos_ilicitos"


@strawberry.enum
class TipoEntidadHecho(Enum):
    HECHO_JUDICIAL = "hecho_judicial"
    DECLARACION = "declaracion"
