"""Real PostgreSQL checks for new isolated bulk tables and range queries."""
import asyncio
import json
import os
import time
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

import asyncpg
from descargas_app import bulk_transfer as bulk,bulk_worker as worker

class BulkPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dsn=os.environ.get("PGTEST_DSN")
        if not dsn:self.skipTest("Requires ephemeral Postgres in CI")
        self.conn=await asyncpg.connect(dsn)
        await self.conn.execute(bulk.SCHEMA)

    async def asyncTearDown(self):
        if hasattr(self,"conn"):
            await self.conn.close()

    async def test_schema_and_real_sql_album_selection(self):
        job=uuid.uuid4()
        await self.conn.execute(
            """INSERT INTO descarga_bulk_jobs
               (id,bot_id,uid,generation,source_id,source_hash,source_title,
                target_id,target_title,created,updated,status,counts,run_no)
               VALUES($1,5,77,0,-100777,999,'Origen',-100888,'Destino',
                      1,1,'publishing',$2::jsonb,1)""",
            job,json.dumps({kind:2 for kind in bulk.KINDS}))
        await self.conn.execute(
            """INSERT INTO descarga_bulk_runs(job_id,run_no,ranges,selected,started)
               VALUES($1,1,$2::jsonb,2,1)""",
            job,json.dumps({kind:[0,2] for kind in bulk.KINDS}))
        await self.conn.executemany(
            """INSERT INTO descarga_bulk_items
               (job_id,message_id,category,ordinal,album,reply_to,caption)
               VALUES($1,$2,$3,$4,'album1',0,$5)""",
            [(job,100,"photos",1,"foto"),(job,101,"videos",1,"video")])
        s=type("Service",(),{"repo":type("Repo",(),{"pool":self.conn})()})()
        j={"id":job,"run_no":1}
        ranges={kind:[0,2] for kind in bulk.KINDS}
        first=await worker._candidate(s,j,ranges)
        self.assertEqual(first["message_id"],100)
        album=await worker._batch(s,j,first,ranges)
        self.assertEqual([int(x["message_id"]) for x in album],[100,101])
        # Future passes can intentionally republish without deleting old records.
        await self.conn.execute(
            """INSERT INTO descarga_bulk_deliveries
               (job_id,run_no,message_id,category,status,updated)
               VALUES($1,1,100,'photos','published',1)""",job)
        nxt=await worker._candidate(s,j,ranges)
        self.assertEqual(int(nxt["message_id"]),101)

    async def test_schema_preserves_admin_audit_and_run_isolation(self):
        job=uuid.uuid4()
        await self.conn.execute(
            """INSERT INTO descarga_bulk_jobs
               (id,bot_id,uid,generation,source_id,source_title,
                target_id,target_title,created,updated)
               VALUES($1,5,77,0,-100777,'A',-100888,'B',1,1)""",job)
        await self.conn.execute(
            """INSERT INTO descarga_bulk_runs(job_id,run_no,ranges,selected,started)
               VALUES($1,1,'{}'::jsonb,1,1),($1,2,'{}'::jsonb,1,2)""",job)
        await self.conn.executemany(
            """INSERT INTO descarga_bulk_deliveries
               (job_id,run_no,message_id,category,status,archive_id,destination_id,updated)
               VALUES($1,$2,100,'photos','published',301,$3,1)""",
            [(job,1,401),(job,2,402)])
        await self.conn.execute(
            """INSERT INTO descarga_bulk_audit
               (job_id,run_no,message_id,actor,source_id,target_id,
                archive_id,destination_id,caption,created)
               VALUES($1,1,100,77,-100777,-100888,301,401,'Original',1)""",job)
        self.assertEqual(await self.conn.fetchval(
            "SELECT count(*) FROM descarga_bulk_deliveries WHERE job_id=$1",job),2)
        self.assertEqual(await self.conn.fetchval(
            "SELECT caption FROM descarga_bulk_audit WHERE job_id=$1",job),"Original")

    async def test_claim_recovers_only_review_without_uncertain_delivery(self):
        now=time.time()
        safe=uuid.uuid4()
        unsafe=uuid.uuid4()
        bot_id=977733
        for ident in (safe,unsafe):
            await self.conn.execute(
                """INSERT INTO descarga_bulk_jobs
                   (id,bot_id,uid,generation,source_id,source_title,
                    target_id,target_title,created,updated,status,run_no,error)
                   VALUES($1,$2,77,1,-100777,'Origen',-100888,'Destino',
                          $3,$3,'review',1,'El envío requiere revisión.')""",
                ident,bot_id,now-120)
            await self.conn.execute(
                """INSERT INTO descarga_bulk_runs(job_id,run_no,ranges,selected,started)
                   VALUES($1,1,'{}'::jsonb,5,$2)""",ident,now-120)
        await self.conn.execute(
            """INSERT INTO descarga_bulk_deliveries
               (job_id,run_no,message_id,category,status,archive_id,updated)
               VALUES($1,1,520,'photos','archived',400,$2)""",unsafe,now-80)
        svc=NS(bot_id=bot_id,repo=NS(pool=self.conn))
        claimed=await worker._claim(svc)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"],safe)
        self.assertEqual(claimed["status"],"publishing")
        self.assertEqual(claimed["error"],"Recuperación segura automática en curso.")
        # The other transfer has a real archived message with unconfirmed
        # destination; it must NEVER be retried automatically.
        self.assertIsNone(await worker._claim(svc))
        still=await self.conn.fetchrow(
            "SELECT status,error FROM descarga_bulk_jobs WHERE id=$1",unsafe)
        self.assertEqual(still["status"],"review")

    async def test_incomplete_media_retry_preserves_published_messages(self):
        now=time.time()
        safe=uuid.uuid4()
        unsafe=uuid.uuid4()
        bot_id=977734
        for ident in (safe,unsafe):
            await self.conn.execute(
                """INSERT INTO descarga_bulk_jobs
                   (id,bot_id,uid,generation,source_id,source_title,
                    target_id,target_title,created,updated,status,run_no,error,
                     run_total,run_done)
                   VALUES($1,$2,77,1,-100777,'Origen',-100888,'Destino',
                          $3,$3,'error',1,
                          'El archivo descargado no coincide con el tamaño original; no se envió.',
                          336,66)""",ident,bot_id,now-120)
            await self.conn.execute(
                """INSERT INTO descarga_bulk_runs(job_id,run_no,ranges,selected,started)
                   VALUES($1,1,'{}'::jsonb,336,$2)""",ident,now-120)
        await self.conn.execute(
            """INSERT INTO descarga_bulk_deliveries
               (job_id,run_no,message_id,category,status,archive_id,destination_id,updated)
               VALUES($1,1,520,'photos','published',400,500,$2)""",safe,now-90)
        await self.conn.execute(
            """INSERT INTO descarga_bulk_deliveries
               (job_id,run_no,message_id,category,status,archive_id,updated)
               VALUES($1,1,521,'photos','archived',401,$2)""",unsafe,now-80)
        svc=NS(bot_id=bot_id,repo=NS(pool=self.conn))
        selected=await worker._claim(svc)
        self.assertEqual(selected["id"],safe)
        self.assertEqual(selected["status"],"publishing")
        self.assertEqual(selected["run_done"],66)
        self.assertEqual(selected["error"],
                         "Reanudación verificada tras archivo incompleto.")
        self.assertIsNone(await worker._claim(svc))
        still=await self.conn.fetchval(
            "SELECT status FROM descarga_bulk_jobs WHERE id=$1",unsafe)
        self.assertEqual(still,"error")

    async def test_twenty_independent_destination_jobs_and_atomic_limit(self):
        """21 simultaneous requests may create only 20 jobs for one user."""
        bot_id,uid=77777,88888
        dsn=os.environ["PGTEST_DSN"]
        pool=await asyncpg.create_pool(dsn,min_size=1,max_size=5)
        try:
            targets=[-100600-i for i in range(21)]
            await self.conn.executemany(
                """INSERT INTO descarga_bulk_chats
                   (bot_id,chat_id,title,chat_type,seen_at)
                   VALUES($1,$2,$3,'supergroup',1)""",
                [(bot_id,target,f"Destino {i}") for i,target in enumerate(targets)])
            svc=NS(bot_id=bot_id,almacen=-100999,repo=NS(
                pool=pool,policy=AsyncMock(return_value={"locked":False})),
                api=AsyncMock(return_value={"status":"administrator"}))
            with patch.object(bulk,"ensure_allowed",new_callable=AsyncMock),\
                 patch.object(bulk,"check_destination",new_callable=AsyncMock),\
                 patch.object(bulk,"_my_account",new_callable=AsyncMock,
                              return_value={"generation":1}),\
                 patch.object(bulk,"_source_dialog",new_callable=AsyncMock,
                              return_value={"title":"Origen","hash":100}):
                outcomes=await asyncio.gather(*[
                    bulk.create(svc,uid,{"source":-100777,"destination":target})
                    for target in targets],return_exceptions=True)
                successes=[x for x in outcomes if isinstance(x,dict)]
                rejections=[x for x in outcomes if isinstance(x,ValueError)]
                self.assertEqual(len(successes),20)
                self.assertEqual(len(rejections),1)
                self.assertIn("20 transferencias",str(rejections[0]))
                jobs=await bulk.list_jobs(svc,uid)
                self.assertEqual(len(jobs["jobs"]),20)
                with self.assertRaisesRegex(ValueError,"ya tienen una transferencia"):
                    await bulk.create(svc,uid,
                        {"source":-100777,"destination":targets[0]})
                await bulk.disconnect(svc,uid,{"id":successes[0]["id"]})
                # The rejected destination is whichever concurrency request lost.
                rejected_target=targets[next(i for i,x in enumerate(outcomes)
                                            if isinstance(x,ValueError))]
                created=await bulk.create(svc,uid,
                    {"source":-100777,"destination":rejected_target})
                self.assertTrue(created["id"])
                self.assertEqual(len((await bulk.list_jobs(svc,uid))["jobs"]),20)
        finally:
            await pool.close()


if __name__=="__main__":unittest.main()
