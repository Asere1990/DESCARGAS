import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from descargas_app.moderation import command,DownloadBlocked,ensure_allowed
from descargas_app.media_cache import deliver_cached

class ModerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        path=Path(self.directory.name)/'admins.txt';path.write_text('1\n# authorized only\n')
        patcher=patch('descargas_app.moderation.ADMIN_FILE',path);patcher.start();self.addCleanup(patcher.stop)
    def service(self,status='administrator'):
        async def api(method,data):return {'status':status} if method=='getChatMember' else {}
        return SimpleNamespace(bot_id=123,almacen=-100999,api=AsyncMock(side_effect=api),
            repo=SimpleNamespace(set_blocks=AsyncMock(),archive_origin=AsyncMock(return_value={'uid':10,'source':-100123}),is_blocked=AsyncMock(return_value=False)))
    def msg(self,text='/ban',**extra):
        return dict({'message_id':55,'chat':{'id':-100999,'type':'supergroup'},'from':{'id':1},'text':text,'reply_to_message':{'message_id':99}},**extra)
    async def test_reply_blocks_trusted_user_and_source(self):
        s=self.service();self.assertTrue(await command(s,self.msg()))
        s.repo.archive_origin.assert_awaited_once_with(123,-100999,99)
        s.repo.set_blocks.assert_awaited_once_with(123,[10,-100123],True,1)
    async def test_anonymous_admin_cannot_bypass_admins_file(self):
        s=self.service();await command(s,self.msg(sender_chat={'id':-100999},**{'from':{'id':1087968824,'is_bot':True}}))
        s.repo.set_blocks.assert_not_awaited()
    async def test_authorized_dm_and_nonlisted_group_admin(self):
        s=self.service('member');await command(s,self.msg('/ban 10',chat={'id':1,'type':'private'}))
        s.repo.set_blocks.assert_awaited_once_with(123,[10],True,1)
        s=self.service('administrator');await command(s,self.msg('/ban 10',**{'from':{'id':2}}))
        s.repo.set_blocks.assert_not_awaited()

    async def test_separate_ids_and_unban(self):
        for text,target,blocked in [('/ban 10',10,True),('/ban -100123',-100123,True),('/unban 10',10,False),('/unban -100123',-100123,False)]:
            s=self.service();await command(s,self.msg(text));s.repo.set_blocks.assert_awaited_once_with(123,[target],blocked,1)
    async def test_rejects_non_admin_anonymous_other_chats_and_unknown_content(self):
        for s,m in [(self.service('member'),self.msg(**{'from':{'id':2}})),(self.service(),self.msg(sender_chat={'id':-100888})),(self.service(),self.msg(chat={'id':-100888,'type':'supergroup'}))]:
            await command(s,m);s.repo.set_blocks.assert_not_awaited()
        s=self.service();s.repo.archive_origin.return_value=None
        m=self.msg();m['reply_to_message']['caption']='Telegram ID: 999'
        await command(s,m);s.repo.set_blocks.assert_not_awaited()
    async def test_blocked_source_never_looks_up_or_sends_cache(self):
        s=self.service();s.repo.is_blocked.return_value=True
        s.source_items=AsyncMock(return_value=((-100123,5),[]));s.repo.media_cache=AsyncMock()
        with self.assertRaisesRegex(DownloadBlocked,'^Bot fuera de servicio$'):
            await deliver_cached(s,10,None,'link',set())
        s.repo.media_cache.assert_not_awaited();s.api.assert_not_awaited()
    async def test_user_block_is_checked_without_source(self):
        s=self.service();s.repo.is_blocked.return_value=True
        with self.assertRaises(DownloadBlocked):await ensure_allowed(s,10)
        s.repo.is_blocked.assert_awaited_once_with(123,10,None)
