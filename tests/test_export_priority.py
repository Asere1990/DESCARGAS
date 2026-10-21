import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from telethon import errors
from descargas_app import group_download as g
from descargas_app.service import Service
import test_group_download as helpers

class PriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_lookup_survives_new_clients_but_not_account_changes(self):
        context=g.ExportContext(NS(),NS())
        context.export_uid=12;context.export_generation=4
        entity=NS(id=123,bot=False)
        with patch.object(Service,'resolve',AsyncMock(return_value=entity)) as lookup:
            first=object();second=object()
            self.assertIs(await context.resolve(first,-100123),entity)
            self.assertIs(await context.resolve(second,-100123),entity)
            self.assertEqual(lookup.await_count,1)
            context.export_uid=13
            await context.resolve(second,-100123)
            self.assertEqual(lookup.await_count,2)
            context.export_generation=5
            await context.resolve(second,-100123)
            self.assertEqual(lookup.await_count,3)
            await context.resolve(second,-100456)
            self.assertEqual(lookup.await_count,4)

    async def test_failed_lookup_is_not_cached_and_protection_stays_checked(self):
        context=g.ExportContext(NS(),NS())
        context.export_uid=12;context.export_generation=4
        entity=NS(id=123,bot=False)
        with patch.object(Service,'resolve',AsyncMock(side_effect=[errors.FloodWaitError(request=None,capture=24),entity])) as lookup:
            with self.assertRaises(errors.FloodWaitError):await context.resolve(object(),-100123)
            await context.resolve(object(),-100123)
            with patch.object(g,'require_entity',side_effect=ValueError('protected')):
                with self.assertRaisesRegex(ValueError,'protected'):await context.resolve(object(),-100123)
            self.assertEqual(lookup.await_count,2)

    async def test_new_foreground_work_prevents_background_preparation(self):
        s=helpers.GroupExportTests().service()
        item=NS(id=4,message='',photo=NS(id=123),document=None,file=NS(size=1))
        with patch.object(g,'users_busy',AsyncMock(return_value=True)),patch.object(g,'prepare_album',AsyncMock()) as prepare:
            self.assertIsNone(await g.archive_items(s,{'uid':12,'source':-100123},None,(-100123,4),[item],'User'))
        prepare.assert_not_awaited();s.repo.media_publishing.assert_not_awaited()

    async def test_foreground_work_arriving_during_preparation_prevents_upload(self):
        s=helpers.GroupExportTests().service();s.bot=NS(send_file=AsyncMock())
        item=NS(id=4,message='',photo=NS(id=123),document=None,file=NS(size=1),entities=[])
        with patch.object(g,'users_busy',AsyncMock(side_effect=[False,True])),patch.object(g,'checked',AsyncMock(return_value=True)),patch.object(g,'archive_attribution',AsyncMock(return_value={})),patch.object(g,'append_archive_source',AsyncMock()),patch.object(g,'prepare_album',AsyncMock(return_value=['media'])):
            self.assertIsNone(await g.archive_items(s,{'uid':12,'source':-100123},None,(-100123,4),[item],'User'))
        s.bot.send_file.assert_not_awaited();s.repo.media_publishing.assert_not_awaited()

    async def test_dedicated_worker_disables_exports_on_user_workers(self):
        s=helpers.GroupExportTests().service();s.role='worker';s.repo.setting.return_value='enabled'
        self.assertFalse(await g.checked(s,helpers.GroupExportTests().job()))
        s.repo.account.assert_not_awaited()

    async def test_flood_wait_sets_at_least_one_minute_before_manual_restart(self):
        s=helpers.GroupExportTests().service();job=helpers.GroupExportTests().job()
        start=time.time()
        await g.defer_flood(s,job,errors.FloodWaitError(request=None,capture=24))
        sql,*args=s.repo.pool.execute.call_args.args
        self.assertIn('resume_at=GREATEST',sql)
        self.assertGreaterEqual(args[-1],start+60)
        self.assertIn('reactivación será manual',s.tell.call_args.args[1])
        self.assertIn("THEN 'paused'",sql)

    async def test_uncertain_send_is_not_automatically_replayed_after_flood(self):
        s=helpers.GroupExportTests().service();s.repo.pool.fetchval.side_effect=None;s.repo.pool.fetchval.return_value=True
        await g.defer_flood(s,helpers.GroupExportTests().job(),errors.FloodWaitError(request=None,capture=24))
        self.assertIn("state='paused'",s.repo.pool.execute.call_args.args[0])
        self.assertNotIn('resume_at',s.repo.pool.execute.call_args.args[0])

    async def test_cooldown_prevents_reading_session_until_wait_expires(self):
        s=helpers.GroupExportTests().service();job=helpers.GroupExportTests().job()
        s.repo.pool.fetchrow.return_value={**job,'resume_at':time.time()+24}
        with patch.object(g,'authorized',return_value=True):self.assertFalse(await g.checked(s,job))
        s.repo.account.assert_not_awaited()

    async def test_manual_restart_cannot_evade_account_wait_by_using_another_group(self):
        s=helpers.GroupExportTests().service()
        s.repo.pool.fetchval.side_effect=None;s.repo.pool.fetchval.return_value=time.time()+60
        with patch.object(g,'authorized',return_value=True):
            await g.command(s,{'text':'/descargar 12 -1009876543210','chat':{'type':'private'},'from':{'id':99}})
        s.repo.pool.execute.assert_not_awaited()
        self.assertIn('todavía',s.tell.call_args.args[1])

    async def test_longer_flood_wait_gets_extra_margin(self):
        s=helpers.GroupExportTests().service();start=time.time()
        await g.defer_flood(s,helpers.GroupExportTests().job(),errors.FloodWaitError(request=None,capture=120))
        self.assertGreaterEqual(s.repo.pool.execute.call_args.args[-1],start+130)
