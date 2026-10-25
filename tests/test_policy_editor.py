import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from descargas_app import policy_editor as p

class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):p.EXTRA={}
    async def test_append_merges_persistently_and_reloads_without_duplicates(self):
        store={p.KEY:json.dumps({'permisos.txt':[11]})};queries=[]
        async def fetch(key):return store.get(key)
        async def execute(sql,*args):
            queries.append(sql)
            if sql.startswith('INSERT'):store[args[0]]=args[1]
        @asynccontextmanager
        async def context():yield conn
        conn=N(fetchval=AsyncMock(side_effect=lambda sql,key:fetch(key)),execute=execute,transaction=context)
        # AsyncMock return coroutine flattening is not automatic.
        async def query(sql,key):return store.get(key)
        conn.fetchval=query
        s=N(repo=N(pool=N(acquire=context),setting=fetch))
        with patch('descargas_app.creator_access.police',return_value=True):
            result=await p.append(s,99,'permisos.txt','11,22,22')
        self.assertEqual(result,{22});self.assertEqual(p.extra('permisos.txt'),{11,22})
        self.assertIn('pg_advisory_xact_lock',queries[0]);p.EXTRA={};await p.refresh(s)
        self.assertEqual(p.extra('permisos.txt'),{11,22})
    async def test_nonadmin_cannot_append(self):
        with patch('descargas_app.creator_access.police',return_value=False):
            with self.assertRaises(ValueError):await p.append(N(),88,'permisos.txt','22')
    def test_whole_input_is_validated_before_any_change(self):
        for name,text in [('protegidos','123'),('cuentas.txt','-100123'),('permisos.txt','12,bad'),('admins.txt','12'),('permisos.txt','12,,13')]:
            with self.assertRaises(ValueError):p.parse(name,text)
        self.assertEqual(p.parse('protegidos','-100123, -100456'),{-100123,-100456})
    async def test_group_messages_cannot_open_editor(self):
        panel=N(s=N(repo=N(setting=AsyncMock())))
        handled=await p.handle(panel,{'message':{'from':{'id':99},'chat':{'id':-100123,'type':'supergroup'},'text':'12,13'}})
        self.assertFalse(handled);panel.s.repo.setting.assert_not_awaited()
