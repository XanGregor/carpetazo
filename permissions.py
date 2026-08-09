"""
Clases de permiso para permission_classes=[...] en queries y mutations.

Las de la superficie interna leen info.context["usuario"] (un
auth.UsuarioActual, puesto ahí por el context_getter a partir del JWT).
La de la superficie pública lee info.context["api_key"].
"""
from typing import Any

from strawberry.permission import BasePermission
from strawberry.types import Info

from .enums import RolUsuario


class EsUsuarioAutenticado(BasePermission):
    message = "Necesitás iniciar sesión para hacer esto."

    def has_permission(self, source: Any, info: Info, **kwargs: Any) -> bool:
        return info.context.get("usuario") is not None


class EsAdmin(BasePermission):
    message = "Esta acción requiere rol de administrador."

    def has_permission(self, source: Any, info: Info, **kwargs: Any) -> bool:
        usuario = info.context.get("usuario")
        return usuario is not None and usuario.rol == RolUsuario.ADMIN


class EsEditorOAdmin(BasePermission):
    message = "Esta acción requiere rol de editor o administrador."

    def has_permission(self, source: Any, info: Info, **kwargs: Any) -> bool:
        usuario = info.context.get("usuario")
        return usuario is not None and usuario.rol in (RolUsuario.ADMIN, RolUsuario.EDITOR)


class PuedeProponerContenido(BasePermission):
    """
    Cualquier rol interno autenticado puede proponer contenido (colaborador
    incluido) — qué pasa después (se publica directo o queda pendiente de
    aprobación) se decide dentro de la mutation según el rol, no acá.
    """

    message = "Necesitás una cuenta del equipo para proponer contenido."

    def has_permission(self, source: Any, info: Info, **kwargs: Any) -> bool:
        return info.context.get("usuario") is not None


class TieneApiKeyValida(BasePermission):
    message = "Se requiere una API key válida (header X-API-Key). Registrate con solicitarApiKey."

    def has_permission(self, source: Any, info: Info, **kwargs: Any) -> bool:
        return info.context.get("api_key") is not None
