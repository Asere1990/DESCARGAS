"""Real PostgreSQL checks for new isolated bulk tables and range queries."""
import json
import os
import unittest
import uuid

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

if __name__=="__main__":unittest.main()
