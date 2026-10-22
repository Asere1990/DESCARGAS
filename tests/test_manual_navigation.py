import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from test_manual_board import Pool
from descargas_app import manual_navigation as n,manual_board as b
from descargas_app.manual_progress import ManualProgress


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool=Pool();self.addCleanup(self.pool.db.close)
        self.pool.db.execute('CREATE TABLE descarga_settings(key TEXT PRIMARY KEY,value TEXT)')
        async def setting(k):return await self.pool.fetchval('SELECT value FROM descarga_settings WHERE key=$1',k)
        async def put(k,v):await self.pool.execute('INSERT INTO descarga_settings VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=excluded.value',k,str(v))
        self.next_mid=100
        async def api(method,args):
            if method=='sendMessage':self.next_mid+=1;return {'message_id':self.next_mid}
            return True
        self.s=NS(bot_id=7,almacen=-100999,repo=NS(pool=self.pool,encode=lambda d:json.dumps(d).encode(),decode=lambda d:json.loads(d),setting=setting,set_setting=put,account=AsyncMock(return_value={'session':b'x','generation':4})),api=AsyncMock(side_effect=api),tell=AsyncMock())
        self.data={'title':'Grupo','generation':4,'mode':'photo','complete':True,'entries':[{'mid':i,'ids':[i],'photos':1,'videos':0,'gifs':0,'elements':1,'files':[['photo',i]],'status':'saved' if i==53 else 'pending'} for i in range(1,61)]}
        await self.pool.execute('INSERT INTO descarga_manual_boards(bot_id,uid,source,actor,token,payload,message_id,position) VALUES($1,$2,$3,$4,$5,$6,$7,$8)',7,12,-100123,99,'board',self.s.repo.encode(self.data),50,3)
        self.context={'uid':12,'source':-100123,'generation':4,'mode':'photo','query':'','number':53,'saved':True}
        await n.confirm(self.s,99,self.context,1,b.link(-100123,53))
        self.patches=[patch.object(n,'authorized',return_value=True),patch.object(b,'authorized',return_value=True)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    async def query(self,action):
        state,ctx=await n.read(self.s,99)
        return {'data':f'manual:{action}:{ctx["id"]}:{ctx["number"]}','message':{'chat':{'id':99},'message_id':state['last']}}
    async def job(self):return await self.pool.fetchrow('SELECT * FROM descarga_group_exports WHERE bot_id=7')
    async def row(self):return await self.pool.fetchrow('SELECT * FROM descarga_manual_boards WHERE bot_id=7')
    async def finish(self):
        with patch('descargas_app.export_inventory.stored',AsyncMock(return_value=True)):
            ctx=await b.complete(self.s,await self.job(),[],1)
        await n.confirm(self.s,99,ctx,1,b.link(-100123,ctx['number']))
        return ctx

    async def test_confirmation_has_four_buttons_and_survives_state_reload(self):
        state,ctx=await n.read(self.s,99)
        self.assertEqual(ctx['number'],53)
        buttons=n.buttons(ctx)
        self.assertEqual([len(row) for row in buttons],[2,1,1])
        self.assertEqual(sum(len(row) for row in buttons),4)
        self.assertTrue(all(len(button['callback_data'].encode())<=64 for row in buttons for button in row))
        self.assertEqual(state['last'],101)

    async def test_next_next_previous_tracks_last_selected_number_and_preserves_normal_cursor(self):
        await n.callback(self.s,99,await self.query('zn'))
        self.assertEqual((await self.job())['retry_message'],54)
        self.assertEqual((await self.row())['position'],53)
        ctx=await self.finish();self.assertEqual(ctx['number'],54)
        self.assertEqual((await self.row())['position'],3)
        await n.callback(self.s,99,await self.query('zn'))
        self.assertEqual((await self.job())['retry_message'],55)
        await self.finish()
        await n.callback(self.s,99,await self.query('zp')) # 54 is already archived
        _,ctx=await n.read(self.s,99);self.assertEqual(ctx['number'],54)
        self.assertEqual((await self.job())['total'],2)
        await n.callback(self.s,99,await self.query('zp'))
        _,ctx=await n.read(self.s,99);self.assertEqual(ctx['number'],53)
        self.assertFalse((await self.row())['busy'])

    async def test_double_click_cannot_enqueue_twice_and_old_confirmation_is_rejected(self):
        query=await self.query('zn')
        await n.callback(self.s,99,query)
        with self.assertRaises(ValueError):await n.callback(self.s,99,query)
        await self.finish()
        with self.assertRaisesRegex(ValueError,'confirmación ya cambió'):await n.callback(self.s,99,query)
        self.assertEqual((await self.job())['total'],1)

    async def test_first_last_and_session_boundaries_never_download_another_item(self):
        for number,action in ((1,'zp'),(60,'zn')):
            await n.confirm(self.s,99,dict(self.context,number=number),1,b.link(-100123,number))
            with self.assertRaises(ValueError):await n.callback(self.s,99,await self.query(action))
            self.assertIsNone(await self.job())
        self.s.repo.account.return_value={'session':b'x','generation':5}
        with self.assertRaisesRegex(ValueError,'sesión cambió'):await n.callback(self.s,99,await self.query('zp'))
        self.assertIsNone(await self.job())

    async def test_back_turns_confirmation_into_board_without_deleting_it_on_next_notice(self):
        query=await self.query('zb');mid=query['message']['message_id']
        await n.callback(self.s,99,query)
        state,ctx=await n.read(self.s,99)
        self.assertIsNone(ctx);self.assertNotIn('last',state)
        self.assertEqual((await self.row())['message_id'],mid)
        self.assertEqual((await self.row())['position'],3)
        self.s.api.reset_mock()
        await n.confirm(self.s,99,self.context,0,b.link(-100123,53))
        deleted=[c.args[1]['message_id'] for c in self.s.api.call_args_list if c.args[0]=='deleteMessage']
        self.assertNotIn(mid,deleted)

    async def test_menu_returns_to_selected_account_and_retires_navigation(self):
        with patch.object(b,'menu',AsyncMock()) as menu:
            await n.callback(self.s,99,await self.query('zm'))
        menu.assert_awaited_once_with(self.s,99,12)
        state,ctx=await n.read(self.s,99);self.assertIsNone(ctx)
        self.assertEqual((await self.row())['position'],3)

    async def test_progress_updates_clicked_confirmation_for_next_number(self):
        query=await self.query('zn');mid=query['message']['message_id']
        await n.callback(self.s,99,query)
        job=await self.job()
        progress=ManualProgress(self.s,job,[NS(file=NS(size=100))]);progress.update(0,30,100)
        self.s.api.reset_mock();await progress.publish()
        sent=self.s.api.call_args.args[1]
        self.assertEqual(sent['message_id'],mid)
        self.assertIn('54',sent['text']);self.assertIn('30%',sent['text'])
        self.assertEqual(sent['reply_markup']['inline_keyboard'],[])

    async def test_failure_restores_previous_controls_for_retry_and_keeps_cursor(self):
        query=await self.query('zn');mid=query['message']['message_id']
        await n.callback(self.s,99,query)
        job=await self.job()
        await self.pool.execute("UPDATE descarga_group_exports SET state='paused'")
        await b.failed(self.s,job,'Telegram pide esperar')
        self.assertEqual((await self.row())['position'],3)
        state,ctx=await n.read(self.s,99);self.assertEqual(ctx['number'],53)
        calls=[c.args[1] for c in self.s.api.call_args_list if c.args[0]=='editMessageText' and c.args[1]['message_id']==mid]
        self.assertIn('esperar',calls[-1]['text'])
        self.assertEqual(sum(len(row) for row in calls[-1]['reply_markup']['inline_keyboard']),4)

    async def test_first_number_input_already_saved_still_returns_navigation(self):
        from descargas_app import manual_types as types
        await self.s.repo.set_setting('manual-number:7:99',json.dumps({'token':'board','message_id':50,'generation':4,'mode':'photo','expires':9999999999}))
        with patch.object(types,'authorized',return_value=True),patch.object(b,'enqueue',AsyncMock()) as queued:
            self.assertTrue(await types.number_input(self.s,99,'53'))
        queued.assert_not_awaited()
        _,context=await n.read(self.s,99)
        self.assertEqual(context['number'],53)
        self.assertEqual(sum(len(row) for row in n.buttons(context)),4)
