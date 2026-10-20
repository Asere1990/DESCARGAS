"""Telegram command suggestions scoped to authorized administrators' private chats."""
import json
from . import moderation

PUBLIC_COMMANDS = [{'command': 'mi_cuenta', 'description': 'Mi cuenta'}]
ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    {'command': 'start', 'description': 'Abrir el bot'},
    {'command': 'estado', 'description': 'Ver la conexión de mi cuenta'},
    {'command': 'suscripcion', 'description': 'Ver o gestionar mi membresía'},
    {'command': 'cancelar_membresia', 'description': 'Cancelar la renovación de mi membresía'},
    {'command': 'desconectar', 'description': 'Desconectar mi cuenta'},
    {'command': 'datos', 'description': 'Estadísticas y actividad'},
    {'command': 'search', 'description': 'Consultar un usuario: /search telegram_id'},
    {'command': 'usuario', 'description': 'Los 100 usuarios con más descargas'},
    {'command': 'usuarios', 'description': 'Los 100 usuarios con más descargas'},
    {'command': 'grupo', 'description': 'Los 20 grupos o canales con más descargas'},
    {'command': 'grupos', 'description': 'Los 20 grupos o canales con más descargas'},
    {'command': 'telefono', 'description': 'Usuarios por país: /telefono Cuba'},
    {'command': 'telefonos', 'description': 'Usuarios por país: /telefonos Cuba'},
    {'command': 'ban', 'description': 'Bloquear un usuario, grupo o canal: /ban ID'},
    {'command': 'unban', 'description': 'Retirar un bloqueo manual: /unban ID'},
    {'command': 'pay', 'description': 'Exigir membresía a usuario u origen: /pay ID'},
    {'command': 'lock', 'description': 'Excluir copias a ALMACEN y Dropbox: /lock ID'},
    {'command': 'unlock', 'description': 'Permitir copias a ALMACEN y Dropbox: /unlock ID'},
    {'command': 'comando', 'description': 'Ayuda con todos los comandos y ejemplos'},
    {'command': 'comandos', 'description': 'Ayuda con todos los comandos y ejemplos'},
]


async def configure_commands(service):
    await service.api('setMyCommands', {'commands': PUBLIC_COMMANDS})
    try:
        admins = {int(token) for line in moderation.ADMIN_FILE.read_text().splitlines()
                  for token in line.split('#', 1)[0].replace(',', ' ').split()
                  if token.isdecimal() and int(token) > 0}
    except OSError:
        admins = set()
    key = f'command_menu_admins:{service.bot_id}'
    previous = json.loads(await service.repo.setting(key) or '[]')
    for uid in sorted(admins | set(previous)):
        await service.api('setMyCommands', {
            'scope': {'type': 'chat', 'chat_id': uid},
            'commands': ADMIN_COMMANDS if uid in admins else PUBLIC_COMMANDS,
        })
    await service.repo.set_setting(key, json.dumps(sorted(admins)))
