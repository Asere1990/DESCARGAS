"""Reportes MANUALES de mensajes, efectuados solo por el administrador y la sesión vinculada.

La API de Telegram suministra las opciones de cada reporte (no se inventan motivos).
No existen denuncias masivas, automáticas ni desde el bot.
"""
import base64
import hashlib
import json
import time

from aiohttp import web
from telethon import functions, types, utils

from .creator_access import police
from .web_panel import integer

TOKEN_SECONDS = 600
MAX_STEPS = 8


class TelegramReports:
    def __init__(self, panel):
        self.p = panel

    @staticmethod
    def require_admin(actor):
        if not police(actor):
            raise web.HTTPForbidden(text="Solo los administradores pueden enviar reportes.")

    def token(self, actor, uid, generation, source, message, stage, option, depth):
        if not isinstance(option, bytes) or not option or len(option) > 2048:
            raise ValueError("Telegram devolvió una opción de reporte no válida.")
        if depth > MAX_STEPS:
            raise ValueError("Telegram pidió demasiados pasos. Abre el reporte otra vez.")
        return self.p.pack(
            "telegram-report", actor=actor, uid=uid, generation=generation,
            source=source, mid=message, stage=stage,
            option=base64.b64encode(option).decode("ascii"),
            created=int(time.time()), depth=depth,
        )

    def response(self, actor, uid, generation, source, message, value, depth):
        if isinstance(value, types.ReportResultReported):
            return {"kind": "reported"}
        if isinstance(value, types.ReportResultChooseOption):
            options=getattr(value, "options", None) or []
            if not 1 <= len(options) <= 30:
                raise ValueError("Telegram no devolvió motivos de reporte disponibles.")
            choices=[]
            for item in options:
                if not isinstance(item, types.MessageReportOption):
                    raise ValueError("Telegram devolvió un motivo no reconocido.")
                label=str(item.text or "").strip()
                if not label or len(label)>180:
                    raise ValueError("Telegram devolvió un motivo inválido.")
                choices.append({
                    "text": label,
                    "token": self.token(actor, uid, generation, source, message,
                                        "choice", item.option, depth),
                })
            return {"kind": "choose", "title": str(value.title or "Motivo del reporte")[:200],
                    "choices": choices}
        if isinstance(value, types.ReportResultAddComment):
            return {
                "kind": "comment", "optional": bool(getattr(value, "optional", False)),
                "token": self.token(actor, uid, generation, source, message,
                                    "comment", value.option, depth),
            }
        raise ValueError("Telegram devolvió un estado de reporte no reconocido.")

    async def begin(self, actor, payload):
        self.require_admin(actor)
        if not isinstance(payload, dict):
            raise ValueError("Solicitud de reporte no válida.")
        uid=integer(payload.get("account"))
        mid=integer(payload.get("message"))
        handle=payload.get("peer", "")
        self.p.rate("report-open:%s:%s"%(actor,uid), 6)
        async with self.p.account(actor, uid) as (client, generation):
            entity=await self.p.target(actor, uid, generation, client, handle)
            if not isinstance(entity, (types.Chat, types.Channel)):
                raise ValueError("Selecciona un mensaje de un grupo o canal.")
            # No confiar en el message_id recibido por el navegador: comprobar chat.
            await self.p.actions.known_message(client, entity, mid)
            response=await client(functions.messages.ReportRequest(
                peer=entity, id=[mid], option=b"", message=""
            ))
            return self.response(actor,uid,generation,utils.get_peer_id(entity),mid,response,1)

    async def next(self, actor, payload):
        self.require_admin(actor)
        if not isinstance(payload,dict):
            raise ValueError("Solicitud de reporte no válida.")
        uid=integer(payload.get("account"))
        token=payload.get("token")
        state=self.p.unpack(token,"telegram-report")
        if state.get("actor")!=actor or state.get("uid")!=uid:
            raise web.HTTPForbidden(text="El reporte no pertenece a tu sesión.")
        created=state.get("created")
        depth=state.get("depth")
        if not isinstance(created,int) or not isinstance(depth,int) or (
            created>time.time()+30 or time.time()-created>TOKEN_SECONDS or not 1<=depth<=MAX_STEPS
        ):
            raise ValueError("El reporte venció. Abre las opciones del mensaje nuevamente.")
        if state.get("stage") not in ("choice","comment"):
            raise ValueError("Paso de reporte no válido.")
        try:
            option=base64.b64decode(state.get("option",""),validate=True)
        except (ValueError, TypeError):
            raise ValueError("Opción de reporte no válida.") from None
        if not option or len(option)>2048:
            raise ValueError("Opción de reporte no válida.")
        comment=payload.get("comment","")
        if not isinstance(comment,str) or len(comment)>1024:
            raise ValueError("El comentario supera el límite permitido.")
        if state["stage"]=="choice" and comment:
            raise ValueError("Selecciona un motivo antes de escribir el comentario.")
        if state["stage"]=="comment" and not comment.strip() and not payload.get("skip"):
            raise ValueError("Escribe un comentario o selecciona Omitir si Telegram lo permite.")
        if payload.get("skip") and (state["stage"]!="comment" or comment):
            raise ValueError("No se puede omitir esta opción.")
        self.p.rate("report-step:%s:%s"%(actor,uid),12)
        async with self.p.account(actor,uid) as (client,generation):
            entity=await self.p.target(actor,uid,generation,client,payload.get("peer",""))
            source=utils.get_peer_id(entity)
            if (generation!=state.get("generation") or source!=state.get("source")
                or not isinstance(entity,(types.Chat,types.Channel))):
                raise web.HTTPForbidden(text="El grupo, canal o cuenta cambió. Abre el reporte de nuevo.")
            mid=integer(state.get("mid"))
            # Token firmado + llave única: un mismo paso no puede enviarse dos veces,
            # incluso tras reintentos, doble pulsación o reinicio del proceso.
            key="telegram-report-step:%s:%s:%s"%(self.p.s.bot_id,actor,
                    hashlib.sha256(token.encode("utf-8")).hexdigest())
            receipt=await self.p.s.repo.pool.fetchval("""
                INSERT INTO descarga_settings(key,value) VALUES($1,$2)
                ON CONFLICT(key) DO NOTHING RETURNING key
            """,key,json.dumps({"uid":uid,"source":source,"mid":mid,"time":time.time()}))
            if not receipt:
                raise ValueError("Este paso ya se envió. Comprueba Telegram antes de repetir.")
            response=await client(functions.messages.ReportRequest(
                peer=entity,id=[mid],option=option,
                message=comment if state["stage"]=="comment" else ""
            ))
            return self.response(actor,uid,generation,source,mid,response,depth+1)
