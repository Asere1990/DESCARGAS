"""Run once after stopping legacy polling. Never prints credentials or user data."""
import asyncio
import os
from pathlib import Path
import sqlite3
from cryptography.fernet import Fernet
from descargas_app.pg_store import Repository

async def main():
    path=Path(os.environ.get('SQLITE_MIGRATION_PATH','/var/data/descargas/sessions.sqlite3'))
    if not path.is_file():raise RuntimeError('SQLite source not found; no migration was performed')
    cipher=Fernet(os.environ['DESCARGAS_SESSION_KEY'].encode())
    repo=Repository(os.environ['DATABASE_URL'],cipher)
    await repo.start();source=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    try:
        source.execute('BEGIN')
        users={row[0]:row[1] for row in source.execute('SELECT uid,session FROM users')}
        profiles={row[0]:row for row in source.execute('SELECT * FROM profiles')}
        for uid in users.keys()|profiles.keys():
            token=users.get(uid)
            if token:cipher.decrypt(token) # Verify the existing encryption key before import.
            row=profiles.get(uid)
            profile=repo.encode({'phone':cipher.decrypt(row[1]).decode(),'phone_country':row[2],
                'calling_code':row[3],'created_at':row[4],'updated_at':row[5]}) if row else None
            await repo.pool.execute('INSERT INTO descarga_accounts(uid,session,profile) VALUES($1,$2,$3) ON CONFLICT(uid) DO NOTHING',uid,token,profile)
        settings=dict(source.execute('SELECT key,value FROM settings'))
        if 'bot_id' in settings and 'offset' in settings:
            key='offset:'+settings['bot_id']
            if await repo.setting(key) is None:await repo.set_setting(key,settings['offset'])
        print('Migration completed. Existing PostgreSQL records were preserved.')
    finally:source.close();await repo.close()
if __name__=='__main__':asyncio.run(main())
