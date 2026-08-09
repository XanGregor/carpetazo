"""
Inputs de filtro/paginación y de mutaciones, más los tipos de resultado
que devuelve una búsqueda (página de resultados + conteos por faceta,
al estilo del selector de género de Letterboxd).

Lógica de filtrado acordada: dentro de una misma categoría de filtro es OR
(ej: categorías de delito A o B), entre categorías distintas es AND (ej:
categoría de delito Y AND provincia). Eso se implementa en db.py al armar
la consulta SQL — acá solo se define la forma de los filtros.
"""
from datetime import date
from typing import Optional

import strawberry

from .enums import (
    EstadoJudicial,
    NivelFuente,
    TipoDeclaracion,
    TipoFinanciamiento,
    TipoOrganizacion,
    TipoRelacionHecho,
)
from .types import CategoriaDelito, Declaracion, HechoJudicial, Organizacion


# ---------------------------------------------------------------------------
# Filtros de búsqueda
# ---------------------------------------------------------------------------

@strawberry.input(description="Filtros para buscar hechos judiciales. Cada lista es OR interno; entre campos distintos es AND.")
class FiltroHechoJudicial:
    categorias_delito_ids: Optional[list[strawberry.ID]] = None
    organizaciones_ids: Optional[list[strawberry.ID]] = None
    estados_judiciales: Optional[list[EstadoJudicial]] = None
    provincias_ids: Optional[list[strawberry.ID]] = None
    fecha_desde: Optional[date] = None
    fecha_hasta: Optional[date] = None
    texto: Optional[str] = None


@strawberry.input(description="Filtros para buscar declaraciones/votos.")
class FiltroDeclaracion:
    tipos: Optional[list[TipoDeclaracion]] = None
    organizaciones_ids: Optional[list[strawberry.ID]] = None
    provincias_ids: Optional[list[strawberry.ID]] = None
    fecha_desde: Optional[date] = None
    fecha_hasta: Optional[date] = None
    texto: Optional[str] = None


@strawberry.input(description="Filtros para buscar organizaciones (incluye origen de financiamiento, relevante para ONGs).")
class FiltroOrganizacion:
    tipos: Optional[list[TipoOrganizacion]] = None
    tipos_financiamiento: Optional[list[TipoFinanciamiento]] = None
    provincias_ids: Optional[list[strawberry.ID]] = None
    texto: Optional[str] = None


@strawberry.input
class Paginacion:
    cursor: Optional[str] = None
    limite: int = 20


# ---------------------------------------------------------------------------
# Resultados: página + conteos por faceta (para el UI tipo Letterboxd)
# ---------------------------------------------------------------------------

@strawberry.type(description="Una opción de filtro con su conteo de resultados en el contexto de búsqueda actual.")
class OpcionConteo:
    valor: str
    etiqueta: str
    cantidad: int


@strawberry.type
class FacetasHechoJudicial:
    categorias_delito: list[OpcionConteo]
    estados_judiciales: list[OpcionConteo]
    provincias: list[OpcionConteo]


@strawberry.type
class FacetasDeclaracion:
    tipos: list[OpcionConteo]
    provincias: list[OpcionConteo]


@strawberry.type
class PaginaHechosJudiciales:
    items: list[HechoJudicial]
    cursor_siguiente: Optional[str]
    hay_mas: bool
    total_aproximado: int
    facetas: FacetasHechoJudicial


@strawberry.type
class PaginaDeclaraciones:
    items: list[Declaracion]
    cursor_siguiente: Optional[str]
    hay_mas: bool
    total_aproximado: int
    facetas: FacetasDeclaracion


# ---------------------------------------------------------------------------
# Inputs de mutación
# ---------------------------------------------------------------------------

@strawberry.input
class FuenteInput:
    nivel: NivelFuente
    tipo_documento: Optional[str] = None
    url: Optional[str] = None
    medio_institucion: Optional[str] = None
    fecha_publicacion: Optional[date] = None


@strawberry.input
class PersonaEnHechoInput:
    persona_id: strawberry.ID
    rol_id: strawberry.ID


@strawberry.input
class OrganizacionEnHechoInput:
    organizacion_id: strawberry.ID
    rol_id: strawberry.ID


@strawberry.input(description="Requiere al menos una fuente (regla de negocio: nada se publica sin respaldo).")
class CrearHechoJudicialInput:
    titulo: str
    descripcion: str
    categoria_delito_id: strawberry.ID
    estado_judicial: EstadoJudicial
    fecha_hecho: Optional[date] = None
    provincia_id: Optional[strawberry.ID] = None
    fuentes: list[FuenteInput] = strawberry.field(default_factory=list)
    personas: list[PersonaEnHechoInput] = strawberry.field(default_factory=list)
    organizaciones: list[OrganizacionEnHechoInput] = strawberry.field(default_factory=list)


@strawberry.input
class EditarHechoJudicialInput:
    id: strawberry.ID
    titulo: Optional[str] = None
    descripcion: Optional[str] = None
    categoria_delito_id: Optional[strawberry.ID] = None
    estado_judicial: Optional[EstadoJudicial] = None
    fecha_hecho: Optional[date] = None
    provincia_id: Optional[strawberry.ID] = None
    motivo_cambio: Optional[str] = None  # queda en el commit del audit log


@strawberry.input(description="Requiere al menos una fuente.")
class CrearDeclaracionInput:
    titulo: str
    descripcion: str
    tipo: TipoDeclaracion
    fecha: Optional[date] = None
    provincia_id: Optional[strawberry.ID] = None
    fuentes: list[FuenteInput] = strawberry.field(default_factory=list)
    personas: list[PersonaEnHechoInput] = strawberry.field(default_factory=list)
    organizaciones: list[OrganizacionEnHechoInput] = strawberry.field(default_factory=list)


@strawberry.input
class EditarDeclaracionInput:
    id: strawberry.ID
    titulo: Optional[str] = None
    descripcion: Optional[str] = None
    tipo: Optional[TipoDeclaracion] = None
    fecha: Optional[date] = None
    provincia_id: Optional[strawberry.ID] = None
    motivo_cambio: Optional[str] = None


@strawberry.input(description="Vincula dos hechos ya existentes (de cualquier combinación judicial/declaración) con un tipo de relación causal.")
class CrearRelacionHechoInput:
    origen_tipo: str  # "hecho_judicial" | "declaracion"
    origen_id: strawberry.ID
    destino_tipo: str
    destino_id: strawberry.ID
    tipo_relacion: TipoRelacionHecho
    descripcion: Optional[str] = None
    fuente: Optional[FuenteInput] = None


@strawberry.input
class ReportarHechoInput:
    hecho_tipo: str  # "hecho_judicial" | "declaracion"
    hecho_id: strawberry.ID
    descripcion_problema: str
    email_reportante: Optional[str] = None


@strawberry.input
class SolicitarApiKeyInput:
    nombre: str
    email: str
    uso_previsto: Optional[str] = None


@strawberry.input
class IniciarSesionInput:
    email: str
    password: str
