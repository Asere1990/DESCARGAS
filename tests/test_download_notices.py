import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from descargas_app.download_notices import latest_confirmation

class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        settings={}
        async def execute(sql,*args):
            if sql.startswith('INSERT'):settings[args[0]]=args[1]
        conn=NS(execute=AsyncMock(side_effect=execute),fetchval=AsyncMock(side_effect=lambda sql,key:settings.get(key)))
        @asynccontextmanager
        async def acquire():yield conn
        s=NS(bot_id=7,repo=NS(pool=NS(acquire=acquire)),api=AsyncMock(return_value={'message_id':10}))
        return s,settings

    async def test_second_confirmation_deletes_previous_only_after_sending(self):
        s,settings=self.service()
        await latest_confirmation(s,99,'Primera')
        s.api.return_value={'message_id':11}
        await latest_confirmation(s,99,'Segunda')
        calls=s.api.call_args_list
        self.assertEqual([c.args[0] for c in calls],['sendMessage','sendMessage','deleteMessage'])
        self.assertEqual(calls[-1].args[1],{'chat_id':99,'message_id':10})
        self.assertEqual(json.loads(settings['link-download-confirmation:7:99'])['last'],11)

    async def test_failed_send_keeps_previous_confirmation(self):
        s,settings=self.service();await latest_confirmation(s,99,'Primera')
        s.api.reset_mock();s.api.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):await latest_confirmation(s,99,'Segunda')
        self.assertEqual(s.api.await_count,1)
        self.assertEqual(json.loads(settings['link-download-confirmation:7:99'])['last'],10)

    async def test_pending_cleanup_survives_restart_and_is_retried(self):
        s,settings=self.service();await latest_confirmation(s,99,'Primera')
        s.api.side_effect=[{'message_id':11},TimeoutError()]
        await latest_confirmation(s,99,'Segunda')
        self.assertEqual(json.loads(settings['link-download-confirmation:7:99'])['cleanup'],[10])
        s.api=AsyncMock(return_value={'message_id':12})
        await latest_confirmation(s,99,'Tercera')
        deleted=[c.args[1]['message_id'] for c in s.api.call_args_list if c.args[0]=='deleteMessage']
        self.assertEqual(deleted,[10,11])

    async def test_administrators_have_separate_confirmation_slots(self):
        s,settings=self.service()
        await latest_confirmation(s,99,'A');await latest_confirmation(s,98,'B')
        self.assertEqual([c.args[0] for c in s.api.call_args_list],['sendMessage','sendMessage'])
        self.assertEqual(len(settings),2)
