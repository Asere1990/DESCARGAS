import asyncio,json,time,unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from descargas_app.admin_panel import AdminPanel
from descargas_app import miniapp_editor as e

class EditorFlowTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.settings={}
  async def get(k):return self.settings.get(k)
  async def put(k,v):self.settings[k]=v
  self.repo=N(setting=get,set_setting=put,pool=N(),miniapp_copy_values=AsyncMock())
  self.s=N(bot_id=7,repo=self.repo,api=AsyncMock(),almacen=-1009)
  self.p=AdminPanel(self.s);self.p.send=AsyncMock();self.p.pages=AsyncMock()
  self.catalog={mode:{'button':{'text':'Texto original','category':'botones','label':'Botón'}} for mode in e.MODES}
  for module in ('descargas_app.miniapp_editor','descargas_app.miniapp_copy'):
   p=patch(module+'.definitions',return_value=self.catalog);p.start();self.addCleanup(p.stop)
  p=patch.object(e,'values',AsyncMock(return_value={'button':'Texto original'}));p.start();self.addCleanup(p.stop)
  self.save=AsyncMock(return_value=True);p=patch.object(e,'save',self.save);p.start();self.addCleanup(p.stop)
  self.role=patch.object(e,'police',return_value=True);self.role.start();self.addCleanup(self.role.stop)
 def message(self,text,actor=99):return {'update_id':50,'message':{'from':{'id':actor},'chat':{'id':actor,'type':'private'},'text':text}}
 def callback(self,data,actor=99):return {'update_id':49,'callback_query':{'id':'q','from':{'id':actor},'message':{'message_id':1,'chat':{'id':actor,'type':'private'}},'data':data}}
 async def drain(self):
  while self.p.tasks:await asyncio.gather(*list(self.p.tasks))
 async def test_open_choose_app_category_number_and_new_message(self):
  self.assertTrue(await e.handle(self.p,self.callback('copy:open')));await self.drain()
  data=await e.state(self.s,99)
  await e.handle(self.p,self.callback(f'copy:{data["token"]}:app:cuenta'));await self.drain()
  data=await e.state(self.s,99)
  await e.handle(self.p,self.callback(f'copy:{data["token"]}:cat:botones'));await self.drain()
  await e.handle(self.p,self.message('1'));await self.drain()
  self.assertEqual((await e.state(self.s,99))['stage'],'value')
  await e.handle(self.p,self.message('Mi botón nuevo'));await self.drain()
  self.save.assert_awaited_once_with(self.repo,7,99,'cuenta','button','Mi botón nuevo',50)
  self.assertIn('Texto guardado',str(self.p.send.call_args_list))
 async def test_other_user_and_stale_buttons_cannot_modify_selected_text(self):
  await e.listing(self.p,99,'cuenta','botones');old=(await e.state(self.s,99))['token']
  await e.categories(self.p,99,'cuenta')
  await e.handle(self.p,self.callback(f'copy:{old}:cat:botones'));await self.drain()
  self.save.assert_not_awaited();self.assertIn('venció',str(self.p.send.call_args_list))
  with patch.object(e,'police',return_value=False):
   self.assertTrue(await e.handle(self.p,self.callback('copy:open',88)))
  self.save.assert_not_awaited()
 async def test_menu_and_other_bot_controls_cancel_editor_capture(self):
  await e.listing(self.p,99,'cuenta','botones')
  self.assertFalse(await e.handle(self.p,self.message('/menu')));self.assertIsNone(await e.state(self.s,99))
  await e.listing(self.p,99,'cuenta','botones')
  self.assertFalse(await e.handle(self.p,self.callback('stats:account_menu_list')))
  self.assertIsNone(await e.state(self.s,99))
 async def test_editor_never_consumes_ordinary_user_messages(self):
  with patch.object(e,'police',return_value=False):
   self.assertFalse(await e.handle(self.p,self.message('1',88)))
  self.p.send.assert_not_awaited();self.save.assert_not_awaited()
