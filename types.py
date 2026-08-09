"""
Tipos GraphQL que representan las entidades del dominio.

Cada tabla del esquema SQL tiene su tipo espejo acá. Los campos que son
relaciones (ej: Persona.afiliaciones, HechoJudicial.fuentes) se resuelven
con un @strawberry.field async, que llama a un DataLoader (ver
dataloaders.py) en vez de hacer una query directa — así, si un cliente
pide 50 personas con sus afiliaciones en una sola consulta, se hace UNA
query batched en vez de 50 queries individuales (problema N+1).

Los campos *_id que son claves foráneas internas se marcan con
strawberry.Private para que NO aparezcan en el schema GraphQL — son un
detalle de implementación, el cliente navega la relación con el campo
resuelto (ej: `organizacion { nombre }`), no con un id suelto.
"""
from datetime import date
from typing import Annotated, Optional, Union

import strawberry

from .enums import (
    EstadoJudicial,
    EstadoPublicacion,
    NivelFuente,
    TipoDeclaracion,
    TipoFinanciamiento,
    TipoOrganizacion,
    TipoRelacionHecho,
)


@strawberry.type(description="Provincia argentina, usada para filtrar por jurisdicción.")
class Provincia:
    id: strawberry.ID
    nombre: str


@strawberry.type(description="Rol que cumple una persona u organización dentro de un hecho (ej: acusado, denunciante).")
class RolEnHecho:
    id: strawberry.ID
    nombre: str


@strawberry.type(description="Categoría de delito, con hasta dos niveles (categoría general → subcategoría).")
class CategoriaDelito:
    id: strawberry.ID
    nombre: str
    categoria_padre_id: strawberry.Private[Optional[int]]

    @strawberry.field(description="Categoría general si esta es una subcategoría; null si ya es de nivel 1.")
    async def categoria_padre(self, info: strawberry.Info) -> Optional["CategoriaDelito"]:
        if self.categoria_padre_id is None:
            return None
        return await info.context["dataloaders"].categoria_delito.load(self.categoria_padre_id)


@strawberry.type(description="Documento o publicación que respalda un hecho, clasificado por nivel de confiabilidad (A=primaria .. E=otras).")
class Fuente:
    id: strawberry.ID
    nivel: NivelFuente
    tipo_documento: Optional[str]
    url: Optional[str]
    medio_institucion: Optional[str]
    fecha_publicacion: Optional[date]
    hash_archivo: Optional[str]


@strawberry.type(description="Registro de financiamiento de una organización (relevante sobre todo para ONGs/fundaciones).")
class Financiamiento:
    id: strawberry.ID
    tipo: TipoFinanciamiento
    descripcion: Optional[str]
    fecha: Optional[date]

    @strawberry.field
    async def fuentes(self, info: strawberry.Info) -> list[Fuente]:
        return await info.context["dataloaders"].fuentes_por_financiamiento.load(int(self.id))


@strawberry.type(description="Vínculo histórico entre una persona y una organización (partido, empresa, etc.), con vigencia temporal.")
class Afiliacion:
    id: strawberry.ID
    cargo: Optional[str]
    fecha_inicio: date
    fecha_fin: Optional[date]
    persona_id: strawberry.Private[int]
    organizacion_id: strawberry.Private[int]

    @strawberry.field
    async def persona(self, info: strawberry.Info) -> "Persona":
        return await info.context["dataloaders"].persona.load(self.persona_id)

    @strawberry.field
    async def organizacion(self, info: strawberry.Info) -> "Organizacion":
        return await info.context["dataloaders"].organizacion.load(self.organizacion_id)


@strawberry.type(description="Persona de relevancia pública documentada en el archivo.")
class Persona:
    id: strawberry.ID
    codigo: str
    nombre_completo: str
    alias: list[str]
    fecha_nacimiento: Optional[date]
    foto_url: Optional[str]
    bio: Optional[str]
    provincia_id: strawberry.Private[Optional[int]]

    @strawberry.field
    async def provincia(self, info: strawberry.Info) -> Optional[Provincia]:
        if self.provincia_id is None:
            return None
        return await info.context["dataloaders"].provincia.load(self.provincia_id)

    @strawberry.field(description="Todas las afiliaciones históricas de esta persona, pasadas y presentes.")
    async def afiliaciones(self, info: strawberry.Info) -> list[Afiliacion]:
        return await info.context["dataloaders"].afiliaciones_por_persona.load(int(self.id))

    @strawberry.field(description="Hechos judiciales en los que está vinculada esta persona (en cualquier rol).")
    async def hechos_judiciales(self, info: strawberry.Info) -> list["HechoJudicial"]:
        return await info.context["dataloaders"].hechos_judiciales_por_persona.load(int(self.id))

    @strawberry.field(description="Declaraciones/votos en los que está vinculada esta persona.")
    async def declaraciones(self, info: strawberry.Info) -> list["Declaracion"]:
        return await info.context["dataloaders"].declaraciones_por_persona.load(int(self.id))


@strawberry.type(description="Organización: partido político, empresa, ONG, fundación u organismo público.")
class Organizacion:
    id: strawberry.ID
    codigo: str
    nombre: str
    tipo: TipoOrganizacion
    descripcion: Optional[str]
    provincia_id: strawberry.Private[Optional[int]]

    @strawberry.field
    async def provincia(self, info: strawberry.Info) -> Optional[Provincia]:
        if self.provincia_id is None:
            return None
        return await info.context["dataloaders"].provincia.load(self.provincia_id)

    @strawberry.field
    async def afiliaciones(self, info: strawberry.Info) -> list[Afiliacion]:
        return await info.context["dataloaders"].afiliaciones_por_organizacion.load(int(self.id))

    @strawberry.field(description="Registros de origen de financiamiento (principalmente para ONGs/fundaciones).")
    async def financiamiento(self, info: strawberry.Info) -> list[Financiamiento]:
        return await info.context["dataloaders"].financiamiento_por_organizacion.load(int(self.id))

    @strawberry.field
    async def hechos_judiciales(self, info: strawberry.Info) -> list["HechoJudicial"]:
        return await info.context["dataloaders"].hechos_judiciales_por_organizacion.load(int(self.id))

    @strawberry.field
    async def declaraciones(self, info: strawberry.Info) -> list["Declaracion"]:
        return await info.context["dataloaders"].declaraciones_por_organizacion.load(int(self.id))


@strawberry.type(description="Una persona involucrada en un hecho, junto con el rol que cumplió (ej: acusado, denunciante).")
class PersonaEnHecho:
    rol: RolEnHecho
    persona: Persona


@strawberry.type(description="Una organización involucrada en un hecho, junto con el rol que cumplió.")
class OrganizacionEnHecho:
    rol: RolEnHecho
    organizacion: Organizacion


@strawberry.type(description="Hecho de naturaleza judicial (causa, denuncia, condena, etc.).")
class HechoJudicial:
    id: strawberry.ID
    codigo: str
    titulo: str
    descripcion: str
    estado_judicial: EstadoJudicial
    fecha_hecho: Optional[date]
    estado_publicacion: EstadoPublicacion
    categoria_delito_id: strawberry.Private[int]
    provincia_id: strawberry.Private[Optional[int]]

    @strawberry.field
    async def categoria_delito(self, info: strawberry.Info) -> CategoriaDelito:
        return await info.context["dataloaders"].categoria_delito.load(self.categoria_delito_id)

    @strawberry.field
    async def provincia(self, info: strawberry.Info) -> Optional[Provincia]:
        if self.provincia_id is None:
            return None
        return await info.context["dataloaders"].provincia.load(self.provincia_id)

    @strawberry.field
    async def fuentes(self, info: strawberry.Info) -> list[Fuente]:
        return await info.context["dataloaders"].fuentes_por_hecho_judicial.load(int(self.id))

    @strawberry.field
    async def personas(self, info: strawberry.Info) -> list[PersonaEnHecho]:
        return await info.context["dataloaders"].personas_por_hecho_judicial.load(int(self.id))

    @strawberry.field
    async def organizaciones(self, info: strawberry.Info) -> list[OrganizacionEnHecho]:
        return await info.context["dataloaders"].organizaciones_por_hecho_judicial.load(int(self.id))

    @strawberry.field(description="Otros hechos (judiciales o declaraciones) vinculados causalmente a este.")
    async def relaciones(self, info: strawberry.Info) -> list["HechoRelacionado"]:
        return await info.context["dataloaders"].relaciones_por_hecho.load(("hecho_judicial", int(self.id)))


@strawberry.type(description="Hecho no penal pero de interés público: voto legislativo, proyecto de ley, declaración pública, publicación en redes.")
class Declaracion:
    id: strawberry.ID
    codigo: str
    titulo: str
    descripcion: str
    tipo: TipoDeclaracion
    fecha: Optional[date]
    estado_publicacion: EstadoPublicacion
    provincia_id: strawberry.Private[Optional[int]]

    @strawberry.field
    async def provincia(self, info: strawberry.Info) -> Optional[Provincia]:
        if self.provincia_id is None:
            return None
        return await info.context["dataloaders"].provincia.load(self.provincia_id)

    @strawberry.field
    async def fuentes(self, info: strawberry.Info) -> list[Fuente]:
        return await info.context["dataloaders"].fuentes_por_declaracion.load(int(self.id))

    @strawberry.field
    async def personas(self, info: strawberry.Info) -> list[PersonaEnHecho]:
        return await info.context["dataloaders"].personas_por_declaracion.load(int(self.id))

    @strawberry.field
    async def organizaciones(self, info: strawberry.Info) -> list[OrganizacionEnHecho]:
        return await info.context["dataloaders"].organizaciones_por_declaracion.load(int(self.id))

    @strawberry.field(description="Otros hechos (judiciales o declaraciones) vinculados causalmente a este.")
    async def relaciones(self, info: strawberry.Info) -> list["HechoRelacionado"]:
        return await info.context["dataloaders"].relaciones_por_hecho.load(("declaracion", int(self.id)))


# Un hecho relacionado puede apuntar a cualquiera de los dos tipos de hecho.
# Esto es lo que en el frontend dispara el aviso en rojo al principio de la
# ficha cuando existe al menos una relación.
HechoUnion = Annotated[Union[HechoJudicial, Declaracion], strawberry.union("HechoUnion")]


@strawberry.type(description="Relación causal entre dos hechos (ej: una declaración que derivó en una causa judicial).")
class HechoRelacionado:
    tipo_relacion: TipoRelacionHecho
    descripcion: Optional[str]
    hecho: HechoUnion
