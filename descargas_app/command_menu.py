"""Telegram command suggestions scoped to authorized administrators' private chats."""
import json
from . import moderation

PUBLIC_COMMANDS = [{'command': 'mi_cuenta', 'description': 'Mi cuenta'}]
SUPERADMIN_COMMANDS = [{'command': 'menu', 'description': 'Abrir el menú principal'}]
ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    {'command': 'chat', 'description': 'Enviar texto: /chat telegram_id group_id mensaje'},
    {'command': 'descargar', 'description': 'Descargar grupo a ALMACEN: /descargar telegram_id group_id'},
    {'command': 'stop', 'description': 'Pausar grupo: /stop telegram_id group_id'},
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
    {'command': 'grupos', 'description': 'Estadísticas o /grupos telegram_id para listar una cuenta'},
    {'command': 'telefono', 'description': 'Usuarios por país: /telefono Cuba'},
    {'command': 'telefonos', 'description': 'Usuarios por país: /telefonos Cuba'},
    {'command': 'ban', 'description': 'Bloquear un usuario, grupo o canal: /ban ID'},
    {'command': 'unban', 'description': 'Retirar un bloqueo manual: /unban ID'},
    {'command': 'pay', 'description': 'Exigir membresía a usuario u origen: /pay ID'},
    {'command': 'free', 'description': 'Quitar pago individual: /free telegram_id'},
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
    try:
        superadmins = {int(token) for line in moderation.ADMIN_FILE.with_name('police.txt').read_text().splitlines()
                       for token in line.split('#', 1)[0].replace(',', ' ').split()
                       if token.isdecimal() and int(token) > 0}
    except OSError:
        superadmins = set()
    key = f'command_menu_admins:{service.bot_id}'
    previous = json.loads(await service.repo.setting(key) or '[]')
    for uid in sorted(admins | superadmins | set(previous)):
        await service.api('setMyCommands', {
            'scope': {'type': 'chat', 'chat_id': uid},
            'commands': SUPERADMIN_COMMANDS if uid in superadmins else ADMIN_COMMANDS if uid in admins else PUBLIC_COMMANDS,
        })
    await service.repo.set_setting(key, json.dumps(sorted(admins | superadmins)))
