"""
Mutation root. Solo se expone en el schema INTERNO (ver schema.py) — la
superficie pública para terceros es exclusivamente de lectura.

Cada mutation adquiere su propia conexión con
`async with info.context["pool"].acquire() as con:` al empezar, y hace
su transacción sobre esa conexión — no se comparte una conexión entre
mutations ni con los DataLoaders (ver la nota en dataloaders.py sobre por
qué eso rompe con ejecución concurrente de GraphQL).

Dentro de Mutation, cada campo tiene su propio permission_classes: algunos
requieren rol de equipo (crear/editar/aprobar contenido), y dos quedan
abiertos a cualquier visitante del sitio sin cuenta (reportarHecho,
solicitarApiKey) — son las acciones que la propia web pública dispara en
nombre de cualquiera, logueado o no.

Regla de flujo editorial: Admin publica directo; Editor/Colaborador dejan
el hecho en pendiente_aprobacion. Si un Editor edita un hecho YA publicado,
vuelve a pendiente_aprobacion (necesita que un Admin lo revise de nuevo);
si lo edita un Admin, se mantiene publicado. Un Colaborador solo puede
editar sus propias propuestas mientras siguen pendientes.
"""
from __future__ import annotations

import secrets
from typing import Optional

import strawberry

import logging

from . import db, search_engine
from .auth import UsuarioActual, autenticar, crear_token, generar_api_key

logger = logging.getLogger(__name__)
from .enums import EstadoPublicacion, EstadoReporte, RolUsuario
from .inputs import (
    CrearDeclaracionInput,
    CrearHechoJudicialInput,
    CrearRelacionHechoInput,
    EditarDeclaracionInput,
    EditarHechoJudicialInput,
    IniciarSesionInput,
    ReportarHechoInput,
    SolicitarApiKeyInput,
)
from .mappers import to_declaracion, to_hecho_judicial
from .permissions import EsAdmin, EsEditorOAdmin, PuedeProponerContenido
from .types import Declaracion, HechoJudicial, HechoRelacionado


@strawberry.type
class TokenAuth:
    token: str
    usuario_id: strawberry.ID
    rol: RolUsuario


@strawberry.type
class ReporteCreado:
    id: strawberry.ID
    estado: EstadoReporte


@strawberry.type
class ApiKeyCreada:
    id: strawberry.ID
    key: str  # texto plano — se muestra UNA sola vez, no se puede recuperar después
    nombre: str


def _nuevo_hash() -> str:
    # Simplificación consciente respecto al hash de contenido de Git: acá es
    # un identificador aleatorio único por commit, no un hash del contenido.
    # Alcanza para el propósito (trazabilidad de quién cambió qué y cuándo);
    # si más adelante hace falta verificar integridad del contenido en sí,
    # se puede migrar a sha256(contenido) sin romper el resto del esquema.
    return secrets.token_hex(32)


def _estado_inicial(usuario: UsuarioActual) -> tuple[str, Optional[int]]:
    if usuario.rol == RolUsuario.ADMIN:
        return EstadoPublicacion.PUBLICADO.value, usuario.id
    return EstadoPublicacion.PENDIENTE_APROBACION.value, None


async def _verificar_permiso_edicion(con, usuario: UsuarioActual, tabla: str, hecho_id: int) -> dict:
    anterior = await con.fetchrow(f"SELECT * FROM {tabla} WHERE id = $1", hecho_id)
    if anterior is None:
        raise ValueError("El hecho no existe.")
    if usuario.rol == RolUsuario.COLABORADOR:
        if anterior["creado_por"] != usuario.id or anterior["estado_publicacion"] != "pendiente_aprobacion":
            raise PermissionError("Como colaborador solo podés editar tus propias propuestas mientras están pendientes.")
    return anterior


def _estado_tras_edicion(usuario: UsuarioActual, anterior: dict) -> tuple[str, Optional[int]]:
    if usuario.rol == RolUsuario.ADMIN:
        return anterior["estado_publicacion"], anterior["aprobado_por"]
    if anterior["estado_publicacion"] == "publicado":
        return EstadoPublicacion.PENDIENTE_APROBACION.value, None  # requiere re-aprobación
    return anterior["estado_publicacion"], anterior["aprobado_por"]


async def _sincronizar_hj(pool, hecho_id: int) -> None:
    """Se llama SIEMPRE después de que la transacción de escritura ya cerró
    (nunca dentro de ella) — si Meilisearch está caído, un fallo acá no debe
    revertir el cambio en Postgres, que es la fuente de verdad."""
    if not search_engine.habilitado():
        return
    try:
        async with pool.acquire() as con:
            await search_engine.sincronizar_hecho_judicial(con, hecho_id)
    except Exception:
        # una falla de indexación no debe revertir un cambio ya confirmado en
        # Postgres (la fuente de verdad) — pero sí queda logueada para que
        # alguien note que ese hecho quedó desactualizado en el buscador.
        logger.exception("Falló la sincronización con Meilisearch para hecho_judicial id=%s", hecho_id)


async def _sincronizar_decl(pool, declaracion_id: int) -> None:
    if not search_engine.habilitado():
        return
    try:
        async with pool.acquire() as con:
            await search_engine.sincronizar_declaracion(con, declaracion_id)
    except Exception:
        logger.exception("Falló la sincronización con Meilisearch para declaracion id=%s", declaracion_id)


@strawberry.type
class Mutation:
    # -- Autenticación -----------------------------------------------------

    @strawberry.mutation(description="Login del equipo editorial. Devuelve un JWT: usalo como header 'Authorization: Bearer <token>'.")
    async def iniciar_sesion(self, info: strawberry.Info, input: IniciarSesionInput) -> TokenAuth:
        async with info.context["pool"].acquire() as con:
            usuario = await autenticar(con, input.email, input.password)
        if usuario is None:
            raise ValueError("Email o contraseña incorrectos.")
        return TokenAuth(token=crear_token(usuario), usuario_id=str(usuario.id), rol=usuario.rol)

    # -- Hecho judicial ------------------------------------------------------

    @strawberry.mutation(permission_classes=[PuedeProponerContenido])
    async def crear_hecho_judicial(self, info: strawberry.Info, input: CrearHechoJudicialInput) -> HechoJudicial:
        if not input.fuentes:
            raise ValueError("Todo hecho necesita al menos una fuente.")
        usuario: UsuarioActual = info.context["usuario"]
        estado, aprobado_por = _estado_inicial(usuario)

        async with info.context["pool"].acquire() as con, con.transaction():
            fila = await db.insertar_hecho_judicial(
                con,
                titulo=input.titulo,
                descripcion=input.descripcion,
                categoria_delito_id=int(input.categoria_delito_id),
                estado_judicial=input.estado_judicial.value,
                fecha_hecho=input.fecha_hecho,
                provincia_id=int(input.provincia_id) if input.provincia_id else None,
                estado_publicacion=estado,
                creado_por=usuario.id,
                aprobado_por=aprobado_por,
            )
            for f in input.fuentes:
                await db.insertar_fuente(
                    con, hecho_judicial_id=fila["id"], nivel=f.nivel.value, tipo_documento=f.tipo_documento,
                    url=f.url, medio_institucion=f.medio_institucion, fecha_publicacion=f.fecha_publicacion,
                )
            for p in input.personas:
                await db.insertar_vinculo_persona(con, "hecho_judicial", fila["id"], int(p.persona_id), int(p.rol_id))
            for o in input.organizaciones:
                await db.insertar_vinculo_organizacion(con, "hecho_judicial", fila["id"], int(o.organizacion_id), int(o.rol_id))

            commit = await db.insertar_commit(con, hash_=_nuevo_hash(), autor_id=usuario.id, descripcion=f"Creación de {fila['codigo']}")
            await db.insertar_cambio(con, commit_id=commit["id"], entidad_tipo="hecho_judicial", entidad_id=fila["id"], campo="*", valor_anterior=None, valor_nuevo="creado")

        await _sincronizar_hj(info.context["pool"], fila["id"])
        return to_hecho_judicial(fila)

    @strawberry.mutation(permission_classes=[PuedeProponerContenido])
    async def editar_hecho_judicial(self, info: strawberry.Info, input: EditarHechoJudicialInput) -> HechoJudicial:
        usuario: UsuarioActual = info.context["usuario"]
        hecho_id = int(input.id)

        campos = {
            "titulo": input.titulo,
            "descripcion": input.descripcion,
            "categoria_delito_id": int(input.categoria_delito_id) if input.categoria_delito_id else None,
            "estado_judicial": input.estado_judicial.value if input.estado_judicial else None,
            "fecha_hecho": input.fecha_hecho,
            "provincia_id": int(input.provincia_id) if input.provincia_id else None,
        }
        campos = {k: v for k, v in campos.items() if v is not None}
        if not campos:
            raise ValueError("No se indicó ningún campo para editar.")

        async with info.context["pool"].acquire() as con, con.transaction():
            anterior = await _verificar_permiso_edicion(con, usuario, "hecho_judicial", hecho_id)
            nuevo_estado, aprobado_por = _estado_tras_edicion(usuario, anterior)
            campos["estado_publicacion"] = nuevo_estado
            campos["aprobado_por"] = aprobado_por

            set_clause = ", ".join(f"{campo} = ${i + 2}" for i, campo in enumerate(campos))
            nuevo = await con.fetchrow(
                f"UPDATE hecho_judicial SET {set_clause} WHERE id = $1 RETURNING *", hecho_id, *campos.values()
            )

            commit = await db.insertar_commit(con, hash_=_nuevo_hash(), autor_id=usuario.id, descripcion=input.motivo_cambio or f"Edición de {nuevo['codigo']}")
            for campo, valor_nuevo in campos.items():
                if campo in ("estado_publicacion", "aprobado_por"):
                    continue  # se registran como efecto, no como edición pedida por el usuario
                await db.insertar_cambio(
                    con, commit_id=commit["id"], entidad_tipo="hecho_judicial", entidad_id=hecho_id,
                    campo=campo, valor_anterior=str(anterior[campo]), valor_nuevo=str(valor_nuevo),
                )

        await _sincronizar_hj(info.context["pool"], hecho_id)
        return to_hecho_judicial(nuevo)

    @strawberry.mutation(permission_classes=[EsAdmin], description="Publica un hecho pendiente de aprobación.")
    async def aprobar_hecho_judicial(self, info: strawberry.Info, id: strawberry.ID) -> HechoJudicial:
        usuario: UsuarioActual = info.context["usuario"]
        async with info.context["pool"].acquire() as con:
            fila = await db.actualizar_estado_publicacion(con, "hecho_judicial", int(id), EstadoPublicacion.PUBLICADO.value, usuario.id)
        if fila is None:
            raise ValueError("El hecho no existe.")
        await _sincronizar_hj(info.context["pool"], int(id))
        return to_hecho_judicial(fila)

    @strawberry.mutation(permission_classes=[EsAdmin])
    async def rechazar_hecho_judicial(self, info: strawberry.Info, id: strawberry.ID, motivo: str) -> HechoJudicial:
        usuario: UsuarioActual = info.context["usuario"]
        async with info.context["pool"].acquire() as con, con.transaction():
            fila = await db.actualizar_estado_publicacion(con, "hecho_judicial", int(id), EstadoPublicacion.RECHAZADO.value, None)
            if fila is None:
                raise ValueError("El hecho no existe.")
            commit = await db.insertar_commit(con, hash_=_nuevo_hash(), autor_id=usuario.id, descripcion=f"Rechazo: {motivo}")
            await db.insertar_cambio(con, commit_id=commit["id"], entidad_tipo="hecho_judicial", entidad_id=int(id), campo="estado_publicacion", valor_anterior=None, valor_nuevo="rechazado")
        await _sincronizar_hj(info.context["pool"], int(id))  # lo saca del índice, ver sincronizar_hecho_judicial
        return to_hecho_judicial(fila)

    # -- Declaración (misma lógica que hecho judicial, tabla separada) -----

    @strawberry.mutation(permission_classes=[PuedeProponerContenido])
    async def crear_declaracion(self, info: strawberry.Info, input: CrearDeclaracionInput) -> Declaracion:
        if not input.fuentes:
            raise ValueError("Toda declaración necesita al menos una fuente.")
        usuario: UsuarioActual = info.context["usuario"]
        estado, aprobado_por = _estado_inicial(usuario)

        async with info.context["pool"].acquire() as con, con.transaction():
            fila = await db.insertar_declaracion(
                con,
                titulo=input.titulo,
                descripcion=input.descripcion,
                tipo=input.tipo.value,
                fecha=input.fecha,
                provincia_id=int(input.provincia_id) if input.provincia_id else None,
                estado_publicacion=estado,
                creado_por=usuario.id,
                aprobado_por=aprobado_por,
            )
            for f in input.fuentes:
                await db.insertar_fuente(
                    con, declaracion_id=fila["id"], nivel=f.nivel.value, tipo_documento=f.tipo_documento,
                    url=f.url, medio_institucion=f.medio_institucion, fecha_publicacion=f.fecha_publicacion,
                )
            for p in input.personas:
                await db.insertar_vinculo_persona(con, "declaracion", fila["id"], int(p.persona_id), int(p.rol_id))
            for o in input.organizaciones:
                await db.insertar_vinculo_organizacion(con, "declaracion", fila["id"], int(o.organizacion_id), int(o.rol_id))

            commit = await db.insertar_commit(con, hash_=_nuevo_hash(), autor_id=usuario.id, descripcion=f"Creación de {fila['codigo']}")
            await db.insertar_cambio(con, commit_id=commit["id"], entidad_tipo="declaracion", entidad_id=fila["id"], campo="*", valor_anterior=None, valor_nuevo="creado")

        await _sincronizar_decl(info.context["pool"], fila["id"])
        return to_declaracion(fila)

    @strawberry.mutation(permission_classes=[PuedeProponerContenido])
    async def editar_declaracion(self, info: strawberry.Info, input: EditarDeclaracionInput) -> Declaracion:
        usuario: UsuarioActual = info.context["usuario"]
        decl_id = int(input.id)

        campos = {
            "titulo": input.titulo,
            "descripcion": input.descripcion,
            "tipo": input.tipo.value if input.tipo else None,
            "fecha": input.fecha,
            "provincia_id": int(input.provincia_id) if input.provincia_id else None,
        }
        campos = {k: v for k, v in campos.items() if v is not None}
        if not campos:
            raise ValueError("No se indicó ningún campo para editar.")

        async with info.context["pool"].acquire() as con, con.transaction():
            anterior = await _verificar_permiso_edicion(con, usuario, "declaracion", decl_id)
            nuevo_estado, aprobado_por = _estado_tras_edicion(usuario, anterior)
            campos["estado_publicacion"] = nuevo_estado
            campos["aprobado_por"] = aprobado_por

            set_clause = ", ".join(f"{campo} = ${i + 2}" for i, campo in enumerate(campos))
            nuevo = await con.fetchrow(
                f"UPDATE declaracion SET {set_clause} WHERE id = $1 RETURNING *", decl_id, *campos.values()
            )

            commit = await db.insertar_commit(con, hash_=_nuevo_hash(), autor_id=usuario.id, descripcion=input.motivo_cambio or f"Edición de {nuevo['codigo']}")
            for campo, valor_nuevo in campos.items():
                if campo in ("estado_publicacion", "aprobado_por"):
                    continue
                await db.insertar_cambio(
                    con, commit_id=commit["id"], entidad_tipo="declaracion", entidad_id=decl_id,
                    campo=campo, valor_anterior=str(anterior[campo]), valor_nuevo=str(valor_nuevo),
                )

        await _sincronizar_decl(info.context["pool"], decl_id)
        return to_declaracion(nuevo)

    @strawberry.mutation(permission_classes=[EsAdmin])
    async def aprobar_declaracion(self, info: strawberry.Info, id: strawberry.ID) -> Declaracion:
        usuario: UsuarioActual = info.context["usuario"]
        async with info.context["pool"].acquire() as con:
            fila = await db.actualizar_estado_publicacion(con, "declaracion", int(id), EstadoPublicacion.PUBLICADO.value, usuario.id)
        if fila is None:
            raise ValueError("La declaración no existe.")
        await _sincronizar_decl(info.context["pool"], int(id))
        return to_declaracion(fila)

    @strawberry.mutation(permission_classes=[EsAdmin])
    async def rechazar_declaracion(self, info: strawberry.Info, id: strawberry.ID, motivo: str) -> Declaracion:
        usuario: UsuarioActual = info.context["usuario"]
        async with info.context["pool"].acquire() as con, con.transaction():
            fila = await db.actualizar_estado_publicacion(con, "declaracion", int(id), EstadoPublicacion.RECHAZADO.value, None)
            if fila is None:
                raise ValueError("La declaración no existe.")
            commit = await db.insertar_commit(con, hash_=_nuevo_hash(), autor_id=usuario.id, descripcion=f"Rechazo: {motivo}")
            await db.insertar_cambio(con, commit_id=commit["id"], entidad_tipo="declaracion", entidad_id=int(id), campo="estado_publicacion", valor_anterior=None, valor_nuevo="rechazado")
        await _sincronizar_decl(info.context["pool"], int(id))
        return to_declaracion(fila)

    # -- Relación entre hechos ------------------------------------------------

    @strawberry.mutation(
        permission_classes=[EsEditorOAdmin],
        description="Vincula dos hechos existentes con un nexo causal tipado. Requiere Editor o Admin por el peso editorial que tiene afirmar un nexo causal.",
    )
    async def crear_relacion_hecho(self, info: strawberry.Info, input: CrearRelacionHechoInput) -> HechoRelacionado:
        usuario: UsuarioActual = info.context["usuario"]

        async with info.context["pool"].acquire() as con:
            async with con.transaction():
                relacion = await db.insertar_relacion_hecho(
                    con,
                    origen_tipo=input.origen_tipo,
                    origen_id=int(input.origen_id),
                    destino_tipo=input.destino_tipo,
                    destino_id=int(input.destino_id),
                    tipo_relacion=input.tipo_relacion.value,
                    descripcion=input.descripcion,
                )
                if input.fuente:
                    await db.insertar_fuente(
                        con, hecho_relacion_id=relacion["id"], nivel=input.fuente.nivel.value,
                        tipo_documento=input.fuente.tipo_documento, url=input.fuente.url,
                        medio_institucion=input.fuente.medio_institucion, fecha_publicacion=input.fuente.fecha_publicacion,
                    )
                commit = await db.insertar_commit(con, hash_=_nuevo_hash(), autor_id=usuario.id, descripcion=f"Nueva relación entre hechos ({input.tipo_relacion.value})")
                await db.insertar_cambio(con, commit_id=commit["id"], entidad_tipo="hecho_relacion", entidad_id=relacion["id"], campo="*", valor_anterior=None, valor_nuevo="creada")

            tabla = "hecho_judicial" if input.destino_tipo == "hecho_judicial" else "declaracion"
            destino = await con.fetchrow(f"SELECT * FROM {tabla} WHERE id = $1", int(input.destino_id))

        mapper = to_hecho_judicial if input.destino_tipo == "hecho_judicial" else to_declaracion
        return HechoRelacionado(tipo_relacion=input.tipo_relacion, descripcion=input.descripcion, hecho=mapper(destino))

    # -- Acciones abiertas al público (sin cuenta) ---------------------------

    @strawberry.mutation(description="Reporta un error o disputa sobre un hecho publicado. No requiere cuenta.")
    async def reportar_hecho(self, info: strawberry.Info, input: ReportarHechoInput) -> ReporteCreado:
        async with info.context["pool"].acquire() as con:
            fila = await db.insertar_reporte(
                con, hecho_tipo=input.hecho_tipo, hecho_id=int(input.hecho_id),
                descripcion_problema=input.descripcion_problema, email_reportante=input.email_reportante,
            )
        return ReporteCreado(id=str(fila["id"]), estado=EstadoReporte(fila["estado"]))

    @strawberry.mutation(description="Registro para acceso a la API pública de solo lectura. La key se devuelve una sola vez.")
    async def solicitar_api_key(self, info: strawberry.Info, input: SolicitarApiKeyInput) -> ApiKeyCreada:
        key_plana, key_hash = generar_api_key()
        async with info.context["pool"].acquire() as con:
            fila = await db.insertar_api_key(
                con, nombre=input.nombre, email=input.email, uso_previsto=input.uso_previsto, key_hash=key_hash
            )
        return ApiKeyCreada(id=str(fila["id"]), key=key_plana, nombre=fila["nombre"])
