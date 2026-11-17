"""Isolated Mini App group transfer safety tests, with no real Telegram sends."""
import asyncio
import json
import unittest
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from descargas_app import bulk_transfer as bulk,bulk_worker as worker


class SelectionTests(unittest.TestCase):
    def test_strict_per_kind_ranges_and_zero_based_positions(self):
        counts=dict(photos=10000,videos=5000,gifs=1000,photo_files=70,video_files=15)
        values={kind:[0,total] for kind,total in counts.items()}
        values["photos"]=[8000,9000]
        answer=bulk.valid_ranges(counts,values)
        self.assertEqual(answer["photos"],[8000,9000])
        self.assertEqual(sum(y-x for x,y in answer.values()),1000+5000+1000+70+15)
        with self.assertRaisesRegex(ValueError,"Rango"):
            bulk.valid_ranges(counts,{**values,"photos":[9001,9000]})
        with self.assertRaisesRegex(ValueError,"Rango"):
            bulk.valid_ranges(counts,{**values,"photos":[-1,10]})
        with self.assertRaisesRegex(ValueError,"Identificador"):
            bulk._int("123")

    def test_query_uses_correct_bindings_for_mixed_albums(self):
        selected={kind:[0,5] for kind in bulk.KINDS}
        sql,p=worker._range_query(selected)
        self.assertIn("i.ordinal>$3",sql)
        self.assertIn("i.ordinal<=$12",sql)
        self.assertEqual(len(p),10)
        album,album_params=worker._range_query(selected,4)
        self.assertIn("i.ordinal>$4",album)
        self.assertIn("i.ordinal<=$13",album)
        self.assertEqual(album_params,p)

    def test_media_type_distinction(self):
        photo=NS(photo=object(),gif=None,video=None,document=None)
        video=NS(photo=None,gif=None,video=object(),document=None)
        gif=NS(photo=None,gif=True,video=None,document=None)
        picture_document=NS(photo=None,gif=None,video=None,document=NS(mime_type="image/jpeg"))
        video_document=NS(photo=None,gif=None,video=None,document=NS(mime_type="video/mp4"))
        for value,kind in [(photo,"photos"),(video,"videos"),(gif,"gifs"),
                           (picture_document,"photo_files"),(video_document,"video_files")]:
            self.assertEqual(bulk.category(value),kind)


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_channel_and_group_use_linked_session_without_skipping_source_guards(self):
        # Telegram's noforwards flag must not reject a source before scanning.
        # Membership, identity and destination checks remain separate guards.
        @asynccontextmanager
        async def lock(*args,**kwargs):
            yield
        fixtures=[
            (bulk.types.Channel(id=777,title="Canal",photo=bulk.types.ChatPhotoEmpty(),
                                date=None,broadcast=True,access_hash=789,noforwards=True),
             -1000000000777,"channel",789),
            (bulk.types.Chat(id=778,title="Grupo",photo=bulk.types.ChatPhotoEmpty(),
                             participants_count=1,date=None,version=1,noforwards=True),
             -778,"chat",0),
        ]
        for entity,source,kind,access_hash in fixtures:
            async def dialogs(limit):
                yield NS(id=source,name="Origen protegido",entity=entity)
            client=NS(get_me=AsyncMock(return_value=NS(id=77)),
                      iter_dialogs=dialogs,disconnect=AsyncMock())
            svc=NS(repo=NS(account_lock=lock))
            with patch.object(bulk,"_client_for",new_callable=AsyncMock,return_value=client):
                info=await bulk._source_dialog(svc,77,source)
                self.assertEqual((info["title"],info["type"],info["hash"]),
                                 ("Origen protegido",kind,access_hash))
                with self.assertRaisesRegex(ValueError,"no pertenece"):
                    await bulk._source_dialog(svc,77,source-1)
                client.get_me.return_value=NS(id=88)
                with self.assertRaisesRegex(ValueError,"no corresponde"):
                    await bulk._source_dialog(svc,77,source)
            self.assertEqual(client.disconnect.await_count,3)

    async def test_destination_selector_shows_only_groups_where_user_is_admin(self):
        pool=NS(fetch=AsyncMock(return_value=[
          {"chat_id":-1001,"title":"Propio","chat_type":"supergroup"},
          {"chat_id":-1002,"title":"Ajeno","chat_type":"channel"}]))
        svc=NS(bot_id=123,repo=NS(pool=pool),get_session=AsyncMock(return_value="token"))
        with patch.object(bulk,"_my_account",new_callable=AsyncMock) as acct,patch.object(
                bulk,"check_destination",new_callable=AsyncMock,
                side_effect=[True,ValueError("no admin")]) as check:
            result=await bulk.destinations(svc,77)
        self.assertEqual([x["title"] for x in result["groups"]],["Propio"])
        self.assertEqual(check.await_count,2)
        self.assertTrue(acct.awaited)

    async def test_group_registration_ignores_private_messages_and_tracks_membership(self):
        pool=NS(execute=AsyncMock())
        svc=NS(bot_id=777,repo=NS(pool=pool))
        await bulk.observe_chat(svc,{"message":{"chat":{"id":42,"type":"private"},"text":"Hi"}})
        pool.execute.assert_not_awaited()
        await bulk.observe_chat(svc,{"my_chat_member":{
            "chat":{"id":-10077,"type":"supergroup","title":"Prueba"},
            "new_chat_member":{"status":"administrator"}}})
        sql,*params=pool.execute.await_args.args
        self.assertIn("descarga_bulk_chats",sql)
        self.assertEqual(params[:2],[777,-10077])
        self.assertTrue(params[4])
        await bulk.observe_chat(svc,{"my_chat_member":{
            "chat":{"id":-10077,"type":"supergroup","title":"Prueba"},
            "new_chat_member":{"status":"kicked"}}})
        self.assertFalse(pool.execute.await_args.args[5])

    async def test_admin_data_is_exclusively_for_admins_txt(self):
        pool=NS(fetch=AsyncMock(return_value=[]))
        repo=NS(pool=pool)
        svc=NS(bot_id=123,repo=repo)
        with patch.object(bulk,"police",return_value=False):
            with self.assertRaises(PermissionError):
                await bulk.admin(svc,987,{"operation":"status"})
        pool.fetch.assert_not_awaited()
        with patch.object(bulk,"police",return_value=True):
            result=await bulk.admin(svc,55,{"operation":"status"})
        self.assertEqual(result,{"jobs":[]})

    async def test_status_query_is_limited_to_requester_uid(self):
        pool=NS(fetchrow=AsyncMock(return_value=None))
        svc=NS(bot_id=123,repo=NS(pool=pool))
        result=await bulk.current(svc,999)
        self.assertEqual(result,{"job":None})
        sql,*params=pool.fetchrow.await_args.args
        self.assertIn("uid=$2",sql)
        self.assertEqual(params,[123,999])

    async def test_transfer_list_only_includes_requester_and_has_no_admin_storage(self):
        ident=uuid.uuid4()
        pool=NS(fetch=AsyncMock(return_value=[{
            "id":ident,"source_title":"Origen","target_title":"Destino",
            "status":"ready","run_done":0,"run_total":0
        }]))
        svc=NS(bot_id=123,repo=NS(pool=pool))
        result=await bulk.list_jobs(svc,999)
        self.assertEqual(result["limit"],20)
        self.assertEqual(result["jobs"][0]["id"],str(ident))
        self.assertEqual(result["jobs"][0]["destination"],"Destino")
        self.assertNotIn("archive_id",result["jobs"][0])
        sql,*args=pool.fetch.await_args.args
        self.assertIn("bot_id=$1 AND uid=$2",sql)
        self.assertEqual(args,[123,999,20])

    async def test_create_refuses_unsupervised_transfer_before_source_read(self):
        svc=NS(almacen=None,bot_id=123,repo=NS(pool=NS()))
        with patch.object(bulk,"ensure_allowed",new_callable=AsyncMock),patch.object(
                bulk,"_my_account",new_callable=AsyncMock) as acct,patch.object(
                bulk,"check_destination",new_callable=AsyncMock):
            with self.assertRaisesRegex(ValueError,"no está disponible"):
                # Policy check is patched to return only unlocked rules.
                svc.repo.policy=AsyncMock(return_value={"locked":False})
                await bulk.create(svc,77,{"source":-1007,"destination":-1009})
        acct.assert_awaited_once()

    async def test_begin_keeps_prior_delivery_pass_and_block_if_uncertain(self):
        ident=uuid.uuid4()
        job={"id":ident,"bot_id":5,"uid":77,"status":"paused","lease":None,
             "lease_until":0,"pause_until":0,"source_id":-100123,"target_id":-100456,
             "counts":json.dumps({kind:5 for kind in bulk.KINDS}),"run_no":1}
        pool=NS(fetchval=AsyncMock(return_value=1))
        svc=NS(bot_id=5,repo=NS(pool=pool))
        with patch.object(bulk,"_owner",new_callable=AsyncMock,return_value=job),patch.object(
                bulk,"ensure_allowed",new_callable=AsyncMock),patch.object(
                bulk,"check_destination",new_callable=AsyncMock):
            with self.assertRaisesRegex(ValueError,"pendientes de revisión"):
                await bulk.begin(svc,77,{"id":str(ident),"ranges":{kind:[0,3] for kind in bulk.KINDS}})
        pool.fetchval.assert_awaited()
        self.assertIn("status IN ('preparing','archived','review')",pool.fetchval.await_args.args[0])

    async def test_admin_ban_disables_only_bulk_job_for_selected_uid(self):
        pool=NS(execute=AsyncMock())
        repo=NS(pool=pool,set_blocks=AsyncMock())
        svc=NS(bot_id=123,repo=repo)
        with patch.object(bulk,"police",return_value=True):
            await bulk.admin(svc,55,{"operation":"ban","uid":999})
        repo.set_blocks.assert_awaited_once_with(123,[999],True,55)
        query,*args=pool.execute.await_args.args
        self.assertIn("descarga_bulk_jobs",query)
        self.assertEqual(args[:2],[123,999])


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_album_publishing_archives_before_destination_and_preserves_captions(self):
        events=[]
        src=[NS(id=11,file=NS(size=128),message="Foto original",
                entities=[],reply_to_msg_id=0),
             NS(id=12,file=NS(size=188),message="Video original",
                entities=[],reply_to_msg_id=11)]
        client=NS(get_messages=AsyncMock(return_value=src),disconnect=AsyncMock())
        @asynccontextmanager
        async def lock(*args,**kwargs):
            yield
        bot=NS(send_file=AsyncMock(side_effect=lambda *a,**k:(
            events.append("archive") or [NS(id=301),NS(id=302)])))
        repo=NS(account_lock=lock,pace=AsyncMock())
        svc=NS(bot=bot,almacen=-100999,repo=repo)
        job={"id":uuid.uuid4(),"lease":"x","source_id":-1007,
             "source_hash":777,"target_id":-10088,
             "uid":77,"run_no":1,"source_title":"Origen","target_title":"Destino"}
        rows=[{"message_id":11,"category":"photos","sender_id":77,
               "caption":"Foto original","reply_to":0},
              {"message_id":12,"category":"videos","sender_id":79,
               "caption":"Video original","reply_to":11}]
        async def copy(*args):
            events.append("deliver");return [401,402]
        async def save(*args):events.append("checkpoint")
        with patch.object(bulk,"check_destination",new_callable=AsyncMock),patch.object(
                worker,"_source_client",new_callable=AsyncMock,return_value=client),patch.object(
                worker,"_source_peer",return_value="peer"),patch.object(
                worker,"_storage_peer",new_callable=AsyncMock,return_value="archive"),patch.object(
                worker,"_claim_entries",new_callable=AsyncMock),patch.object(
                worker,"_checkpoint_archive",new_callable=AsyncMock,side_effect=save),patch.object(
                worker,"_archive_note",new_callable=AsyncMock),patch.object(
                worker,"_copy_to_target",new_callable=AsyncMock,side_effect=copy),patch.object(
                worker,"_checkpoint_published",new_callable=AsyncMock),patch.object(
                worker,"prepare_album",new_callable=AsyncMock,return_value=["media1","media2"]),patch.object(
                worker.bulk,"category",return_value="photos"):
            await worker.send_media(svc,job,rows)
        self.assertEqual(events,["archive","checkpoint","deliver"])
        text=bot.send_file.await_args.kwargs["caption"]
        self.assertEqual(text,["Foto original","Video original"])
        client.disconnect.assert_awaited_once()

    async def test_busy_linked_account_never_enters_review_before_claim(self):
        job={"id":uuid.uuid4(),"run_no":1,"uid":77,"lease":"lease"}
        item={"message_id":520,"album":"","category":"photos"}
        svc=NS(almacen=-100111,repo=NS(pool=NS(
            fetchrow=AsyncMock(return_value={"ranges":{kind:[0,1] for kind in bulk.KINDS}}))))
        with patch.object(worker,"_inflight",new_callable=AsyncMock,return_value=False),patch.object(
                worker,"_candidate",new_callable=AsyncMock,return_value=item),patch.object(
                worker,"_verify_account",new_callable=AsyncMock),patch.object(
                worker,"send_media",new_callable=AsyncMock,
                side_effect=ValueError("Espera a que termine la transferencia en curso.")),patch.object(
                worker,"_mark_review",new_callable=AsyncMock) as mark:
            with self.assertRaisesRegex(ValueError,"transferencia en curso"):
                await worker.publish_one(svc,job)
            mark.assert_not_awaited()

    async def test_unclaimed_failure_retries_but_ambiguous_send_is_reviewed(self):
        job={"id":uuid.uuid4(),"run_no":1,"uid":77,"lease":"lease"}
        item={"message_id":520,"album":"","category":"photos"}
        svc=NS(almacen=-100111,repo=NS(pool=NS(
            fetchrow=AsyncMock(return_value={"ranges":{kind:[0,1] for kind in bulk.KINDS}}))))
        with patch.object(worker,"_candidate",new_callable=AsyncMock,return_value=item),patch.object(
                worker,"_verify_account",new_callable=AsyncMock),patch.object(
                worker,"send_media",new_callable=AsyncMock,side_effect=TimeoutError("temporary")),patch.object(
                worker,"_mark_review",new_callable=AsyncMock) as mark,patch.object(
                worker,"_inflight",new_callable=AsyncMock,
                side_effect=[False,False,False,True]):
            with self.assertRaises(worker.RetryablePreflight):
                await worker.publish_one(svc,job)
            mark.assert_not_awaited()
            with self.assertRaises(worker.ReviewRequired):
                await worker.publish_one(svc,job)
            mark.assert_awaited_once()

    async def test_ordinary_user_cannot_see_admin_archive_from_public_status(self):
        source=__import__("pathlib").Path("descargas_app/bulk_transfer.py").read_text(encoding="utf-8")
        begin=source.split("async def current(",1)[1].split("def valid_ranges(",1)[0]
        self.assertNotIn("audit",begin.lower())
        self.assertNotIn("archive_id",begin.lower())

if __name__=="__main__":unittest.main()
