import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from cryptography.fernet import Fernet
from descargas_app.pg_store import Repository
from descargas_app.moderation import ensure_allowed
from descargas_app.protected import require_source,require_bot_alias,require_entity,ProtectedSource,BOT_USERNAME

ADMIN=1609522430
class AdminExemptions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        admins=Path(directory.name)/'admins.txt';admins.write_text(str(ADMIN))
        patcher=patch('descargas_app.moderation.ADMIN_FILE',admins);patcher.start();self.addCleanup(patcher.stop)
    async def test_stored_bans_pay_and_lock_do_not_apply_to_admin(self):
        repo=Repository('',Fernet(Fernet.generate_key()))
        repo.pool=NS(fetchval=AsyncMock(return_value=1),fetchrow=AsyncMock(return_value={'pay':True,'locked':True}))
        self.assertFalse(await repo.is_blocked(100,ADMIN,-100123))
        self.assertEqual(await repo.policy(100,ADMIN,-100123),{'pay':False,'locked':False})
        repo.pool.fetchval.assert_not_awaited();repo.pool.fetchrow.assert_not_awaited()
        self.assertTrue(await repo.is_blocked(100,10,-100123))
        self.assertEqual(await repo.policy(100,10,-100123),{'pay':True,'locked':True})
    async def test_access_guard_does_not_apply_membership_or_bans_to_admin(self):
        service=NS(bot_id=100,repo=NS(is_blocked=AsyncMock(return_value=True)),billing=NS(require=AsyncMock()))
        await ensure_allowed(service,ADMIN,-100123)
        service.repo.is_blocked.assert_not_awaited();service.billing.require.assert_not_awaited()
    def test_protected_group_bot_id_and_alias_allow_admin_only(self):
        token=BOT_USERNAME.set(None)
        try:
            with tempfile.TemporaryDirectory() as d:
                groups=Path(d)/'protegidos';groups.write_text('-100123\n')
                bots=Path(d)/'bots_protegidos';bots.write_text('42\n@ProtectedExampleBot\n')
                with patch('descargas_app.protected.FILE',groups),patch('descargas_app.protected.BOTS_FILE',bots):
                    require_source(-100123,ADMIN);require_source(42,ADMIN)
                    require_bot_alias('ProtectedExampleBot',ADMIN)
                    require_entity(NS(id=42,bot=True,username='ProtectedExampleBot'),ADMIN)
                    for action in (lambda:require_source(-100123,10),lambda:require_source(42,10),lambda:require_bot_alias('ProtectedExampleBot',10),lambda:require_entity(NS(id=42,bot=True,username='ProtectedExampleBot'),10)):
                        with self.assertRaises(ProtectedSource):action()
        finally:BOT_USERNAME.reset(token)
