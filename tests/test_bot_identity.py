import os
import tempfile
import unittest
from unittest.mock import patch
from descargas_app.service import Service

class Identity(unittest.TestCase):
    def test_new_bot_resets_updates_preserves_sessions(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'DESCARGAS_DATA_DIR':directory}):
            service=Service()
            try:
                service.bind_bot(100)
                service.db.execute("INSERT INTO users VALUES(10,?)",(b'encrypted-session',))
                service.db.execute("INSERT INTO settings VALUES('offset','999')")
                service.db.execute('INSERT INTO orders VALUES(123,10)');service.db.commit()
                service.bind_bot(100)
                self.assertEqual(service.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],1)
                service.bind_bot(200)
                self.assertIsNone(service.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone())
                self.assertEqual(service.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)
                self.assertEqual(service.db.execute('SELECT session FROM users WHERE uid=10').fetchone()[0],b'encrypted-session')
            finally:service.db.close()
