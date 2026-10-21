import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telethon import types
from descargas_app import export_inventory as inv,group_download as g

class InventoryTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        pool=NS(fetchval=AsyncMock(return_value=None),execute=AsyncMock(),executemany=AsyncMock())
        return NS(bot_id=7,almacen=-100999,api=AsyncMock(),repo=NS(pool=pool,
            archive_origin=AsyncMock(return_value={'uid':12,'source':-100123})))

    def message(self,caption=''):
        return {'message_id':5,'chat':{'id':-100999},'reply_to_message':{'message_id':20,'caption':caption}}

    async def test_lock_older_caption_strips_only_administrative_suffix(self):
        s=self.service();text='Oferta especial\nhttps://ejemplo.com'
        await inv.reply_lock(s,self.message(text+'\n\nUsuario: Persona\nTelegram ID: 12\nGrupo de origen: Origen\nID de origen: -100123'),'lock',99)
        args=s.repo.pool.execute.call_args.args
        self.assertEqual(args[1:],(7,inv.caption_hash(text),99))
        self.assertNotEqual(args[2],inv.caption_hash(text+'\n\nUsuario: Persona'))

    async def test_reply_to_captionless_album_member_uses_registered_caption(self):
        s=self.service();s.repo.pool.fetchval.return_value='trusted-caption-hash'
        await inv.reply_lock(s,self.message(),'lock',99)
        self.assertEqual(s.repo.pool.execute.call_args.args[2],'trusted-caption-hash')

    async def test_empty_original_caption_never_creates_blanket_rule(self):
        s=self.service()
        await inv.reply_lock(s,self.message('Usuario: Persona\nTelegram ID: 12\nGrupo de origen: -100123'),'lock',99)
        s.repo.pool.execute.assert_not_awaited()
        self.assertIn('No hay caption original',s.api.call_args.args[1]['text'])

    async def test_untrusted_album_cannot_create_a_rule(self):
        s=self.service();s.repo.archive_origin.return_value=None
        await inv.reply_lock(s,self.message('Oferta'),'lock',99)
        s.repo.pool.execute.assert_not_awaited()

    async def test_unlock_reply_removes_only_caption_exclusion(self):
        s=self.service();s.repo.pool.fetchval.return_value='hash'
        await inv.reply_lock(s,self.message(),'unlock',99)
        self.assertIn('DELETE FROM descarga_export_caption_locks',s.repo.pool.execute.call_args.args[0])

    def test_matching_is_exact_except_unicode_and_spacing(self):
        self.assertEqual(inv.caption_hash('Cafe\u0301 \nOferta'),inv.caption_hash('Café Oferta'))
        self.assertNotEqual(inv.caption_hash('Oferta'),inv.caption_hash('oferta'))
        self.assertNotEqual(inv.caption_hash('Oferta'),inv.caption_hash('Oferta!'))
        self.assertIsNone(inv.caption_hash(' \n'))

    async def test_excluded_caption_never_prepares_or_uploads_bytes(self):
        s=self.service();s.repo.pool.fetchval.return_value=1
        item=NS(message='Oferta')
        self.assertEqual(await g.archive_items(s,{'uid':12,'source':-100123},None,(-100123,44),[item],'Persona'),0)
        s.repo.pool.executemany.assert_not_awaited()

    async def test_reposted_file_id_is_skipped_independently_of_message_order(self):
        s=self.service();s.repo.pool.fetchval.return_value=1
        item=NS(message='',photo=NS(id=765),document=None)
        self.assertEqual(await g.archive_items(s,{'uid':12,'source':-100123},None,(-100123,999),[item],'Persona'),0)
        self.assertEqual(s.repo.pool.fetchval.call_args.args[-2:],('photo',765))

    async def test_single_failed_file_does_not_prevent_following_file_and_is_not_forgotten(self):
        from test_group_download import GroupExportTests
        helper=GroupExportTests();s=helper.service();job=helper.job()
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None)
        items=[NS(id=i,photo=True,document=None,media=None) for i in [5,6]]
        async def history(*args,**kwargs):
            for m in items:yield m
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),get_me=AsyncMock(return_value=NS(first_name='A',last_name='B')),get_messages=AsyncMock(return_value=[]),iter_messages=history)
        s.client=lambda session:client
        s.source_items.side_effect=[ValueError('El archivo falló'),((job['source'],6),[items[1]])]
        s.repo.pool.fetchrow.return_value={'total':1}
        async def query(sql,*args):return 1 if 'count(*) FROM descarga_export_pending' in sql else False
        s.repo.pool.fetchval.side_effect=query
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'require_entity'),patch.object(g,'archive_items',AsyncMock(return_value=0)) as archive,patch.object(g,'save_cursor',AsyncMock()) as save:
            await g.step(s,job)
        archive.assert_awaited_once()
        self.assertEqual([call.args[2:] for call in save.await_args_list],[(5,),(6,0)])
        inserts=[call for call in s.repo.pool.execute.await_args_list if 'INSERT INTO descarga_export_pending' in call.args[0]]
        self.assertEqual(inserts[0].args[-2:],(5,'ValueError'))
        self.assertIn('Pendientes con error: 1',s.tell.call_args.args[1])

    async def test_reply_lock_does_not_change_existing_id_policy(self):
        from descargas_app.moderation import command
        s=self.service();s.repo.policy_set=AsyncMock()
        message=self.message('Caption');message.update(text='/lock',**{'from':{'id':99}})
        with patch('descargas_app.moderation.authorized',return_value=True),patch.object(inv,'reply_lock',AsyncMock()) as lock:
            await command(s,message)
            lock.assert_awaited_once_with(s,message,'lock',99)
            message['text']='/lock -100123'
            await command(s,message)
        s.repo.policy_set.assert_awaited_once_with(7,-100123,'locked',True,99)

    async def test_legacy_index_scan_restarts_at_zero_once_without_touching_users(self):
        from test_group_download import GroupExportTests
        helper=GroupExportTests();s=helper.service();job={**helper.job(),'phase':'index'}
        s.resolve.return_value=types.Channel(id=123,title='Origen',photo=types.ChatPhotoEmpty(),date=None)
        item=NS(id=5,photo=True,document=None,media=None)
        async def history(*args,**kwargs):yield item
        client=NS(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),get_me=AsyncMock(return_value=NS(first_name='A',last_name='B')),get_messages=AsyncMock(return_value=[]),iter_messages=history)
        s.client=lambda session:client;s.source_items.return_value=((job['source'],5),[item])
        with patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'require_entity'),patch.object(inv,'index_cached',AsyncMock()) as index,patch.object(g,'archive_items',AsyncMock()) as archive:
            await g.step(s,job)
        archive.assert_not_awaited()
        index.assert_not_awaited()
        sql=s.repo.pool.execute.call_args.args[0]
        self.assertIn("phase='download',cursor=0,scan=scan+1",sql)
        self.assertIn("AND NOT sending AND phase='index'",sql)
        client.connect.assert_not_awaited()
