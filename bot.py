import os
import asyncio
from telethon import TelegramClient, errors
from telethon.tl.types import MessageService

# --- CONFIGURACIÓN DE TU CUENTA (Obtenlo en my.telegram.org) ---
API_ID = 1234567          # Reemplaza con tu API ID (numérico)
API_HASH = 'tu_api_hash'  # Reemplaza con tu API Hash (string)
# El nombre del grupo (ej: 'mi_grupo') o el ID (ej: -100123456789)
ENTITY = 'NOMBRE_O_ID_DEL_GRUPO' 

async def descargar_todo():
    # Creamos la sesión (se guardará un archivo .session en la carpeta)
    async with TelegramClient('sesion_respaldo', API_ID, API_HASH) as client:
        print("✅ Conectado exitosamente.")

        # Intentamos obtener el acceso al grupo
        try:
            grupo = await client.get_entity(ENTITY)
            print(f"📂 Accediendo a: {grupo.title}")
        except Exception as e:
            print(f"❌ Error al acceder al grupo: {e}")
            return

        # Creamos una carpeta para las descargas si no existe
        folder = f"descargas_{ENTITY}"
        if not os.path.exists(folder):
            os.makedirs(folder)

        print("🚀 Iniciando descarga masiva... Esto puede tardar dependiendo del volumen.")
        
        count = 0
        # Iteramos por TODOS los mensajes del grupo
        async for message in client.iter_messages(grupo):
            # Ignoramos mensajes de servicio (como "X se unió al grupo")
            if isinstance(message, MessageService) or not message.media:
                continue

            try:
                print(f"📥 Descargando mensaje ID {message.id}...")
                
                # El método mágico que ignora la restricción de "No Guardar"
                path = await client.download_media(
                    message, 
                    file=os.path.join(folder, f"{message.id}_")
                )
                
                if path:
                    print(f"✅ Guardado: {path}")
                    count += 1
                
            except errors.FloodWaitError as e:
                print(f"⏳ Límite de Telegram alcanzado. Esperando {e.seconds} segundos...")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                print(f"⚠️ Error en mensaje {message.id}: {e}")

        print(f"\n✨ ¡Proceso terminado! Se descargaron {count} archivos en la carpeta '{folder}'.")

if __name__ == '__main__':
    asyncio.run(descargar_todo())
