import json,sqlite3,tempfile,unittest,io
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from PIL import Image
from descargas_app import miniapp_copy as c

class Pool:
 def __init__(self):
  self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row;self.db.executescript(c.SCHEMA)
 def query(self,sql,args):return self.db.execute(sql,{str(i):v for i,v in enumerate(args,1)})
 async def execute(self,sql,*args):
  if 'pg_advisory' not in sql:return self.query(sql,args)
 async def executemany(self,sql,rows):
  for row in rows:self.query(sql,row)
 async def fetch(self,sql,*args):return [dict(row) for row in self.query(sql,args).fetchall()]
 async def fetchrow(self,sql,*args):
  row=self.query(sql,args).fetchone();return dict(row) if row else None
 async def fetchval(self,sql,*args):
  row=self.query(sql,args).fetchone();return row[0] if row else None
 @asynccontextmanager
 async def acquire(self):yield self
 @asynccontextmanager
 async def transaction(self):
  self.db.commit();self.db.execute('BEGIN')
  try:yield
  except BaseException:self.db.rollback();raise
  else:self.db.commit()

def image(color):
 out=io.BytesIO();Image.new('RGB',(3,3),color).save(out,format='PNG');return out.getvalue()

class CopyStorageTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.file=self.root/'copy.json';self.data={mode:{'button':{'text':'Original','label':'Botón','category':'botones'}} for mode in c.MODES};self.write()
  for mode in c.MODES:(self.root/('logo-'+mode+'.jpg')).write_bytes(image('red'))
  for name,value in [('FILE',self.file),('WEB',self.root)]:
   p=patch.object(c,name,value);p.start();self.addCleanup(p.stop)
  self.pool=Pool();self.addCleanup(self.pool.db.close);self.repo=N(pool=self.pool)
  await c.activate(self.repo,7)
 def write(self):self.file.write_text(json.dumps(self.data))
 async def test_bot_then_code_then_bot_last_change_wins_per_field(self):
  await c.save(self.repo,7,99,'cuenta','button','Desde el bot',1)
  await c.activate(self.repo,7)
  self.assertEqual((await c.values(self.repo,7,'cuenta'))['button'],'Desde el bot')
  self.data['cuenta']['button']['text']='Desde código';self.write();await c.activate(self.repo,7)
  self.assertEqual((await c.values(self.repo,7,'cuenta'))['button'],'Desde código')
  await c.save(self.repo,7,99,'cuenta','button','Último cambio',2)
  self.assertEqual((await c.values(self.repo,7,'cuenta'))['button'],'Último cambio')
 async def test_other_app_edit_and_unrelated_deploy_do_not_erase_override(self):
  await c.save(self.repo,7,99,'cuenta','button','Personalizado',1)
  self.data['creadores']['button']['text']='Otro cambio';self.write();await c.activate(self.repo,7)
  self.assertEqual((await c.values(self.repo,7,'cuenta'))['button'],'Personalizado')
  self.assertEqual((await c.values(self.repo,7,'creadores'))['button'],'Otro cambio')
 async def test_old_instance_reads_cannot_restore_defaults(self):
  self.data['cuenta']['button']['text']='Código nuevo';self.write();await c.activate(self.repo,7)
  await c.save(self.repo,7,99,'cuenta','button','Edición posterior',1)
  self.data['cuenta']['button']['text']='Original';self.write()
  self.assertEqual((await c.values(self.repo,7,'cuenta'))['button'],'Edición posterior')
 async def test_creator_html_edits_and_legacy_js_keys_share_latest_visible_value(self):
  for field,alias in c.CREATOR_ALIASES.items():
   self.data['creadores'][field]={'text':'HTML original','label':'Visible','category':'avisos'}
   self.data['creadores'][alias]={'text':'Creator original','label':'Legacy','category':'avisos'}
  self.write();await c.activate(self.repo,7)
  for i,(field,alias) in enumerate(c.CREATOR_ALIASES.items()):
   initial=await c.values(self.repo,7,'creadores')
   self.assertEqual(initial[field],'Creator original')
   await c.save(self.repo,7,99,'creadores',field,'Cambio ya guardado',100+i)
   result=await c.values(self.repo,7,'creadores')
   self.assertEqual(result[field],result[alias]);self.assertEqual(result[alias],'Cambio ya guardado')
   self.data['creadores'][alias]['text']='Cambio posterior en código';self.write();await c.activate(self.repo,7)
   result=await c.values(self.repo,7,'creadores')
   self.assertEqual(result[field],'Cambio posterior en código')
   await c.save(self.repo,7,99,'creadores',field,'Último desde editor',200+i)
   await c.activate(self.repo,7)
   result=await c.values(self.repo,7,'creadores')
   self.assertEqual(result[alias],'Último desde editor')
   self.assertIn(field,c.editor_entries('creadores'));self.assertNotIn(alias,c.editor_entries('creadores'))
  rendered=c.render_index('<head></head><p>{{copy:html.011}}</p>',result)
  self.assertIn('<p>Último desde editor</p>',rendered)
 async def test_replayed_update_cannot_overwrite_a_later_edit(self):
  await c.save(self.repo,7,99,'cuenta','button','Primero',1);await c.save(self.repo,7,99,'cuenta','button','Último',2)
  self.assertFalse(await c.save(self.repo,7,99,'cuenta','button','Repetido',1))
  self.assertEqual((await c.values(self.repo,7,'cuenta'))['button'],'Último')
 async def test_logo_independence_restart_and_code_priority(self):
  green=image('green');await c.save_logo(self.repo,7,99,'creadores',green,1);await c.activate(self.repo,7)
  self.assertEqual((await c.read_logo(self.repo,7,'creadores'))[0],green)
  self.assertNotEqual((await c.read_logo(self.repo,7,'cuenta'))[0],green)
  blue=image('blue');(self.root/'logo-creadores.jpg').write_bytes(blue);await c.activate(self.repo,7)
  self.assertEqual((await c.read_logo(self.repo,7,'creadores'))[0],blue)
 async def test_invalid_fields_long_text_and_invalid_images_are_rejected(self):
  for mode,key,value in [('invalid','button','A'),('cuenta','missing','A'),('cuenta','button','x'*4097)]:
   with self.assertRaises(ValueError):await c.save(self.repo,7,99,mode,key,value,1)
  for data in [b'<svg onload="alert(1)"></svg>',b'x'*(5*1024*1024+1)]:
   with self.assertRaises(ValueError):await c.save_logo(self.repo,7,99,'cuenta',data,2)

class RenderingTests(unittest.TestCase):
 def test_all_html_copy_slots_and_attributes_render_without_html_injection(self):
  text='<script>alert(1)</script>"&'
  result=c.render_index('<head></head><p>{{copy:title}}</p><input data-copy-placeholder="title" placeholder="">',{'title':text})
  self.assertIn('&lt;script&gt;',result);self.assertNotIn('<script>alert',result)
  self.assertIn('placeholder="&lt;script&gt;',result);self.assertIn('\\u003cscript',result)
 def test_backend_templates_keep_dynamic_fields_and_editable_words(self):
  entries={'a':{'text':'Original {until}','match':'Renovación hasta {until}.'}}
  self.assertEqual(c.translated('Renovación hasta 23-Oct-2026.',entries,{'a':'Vence {until}'}),'Vence 23-Oct-2026')
 def test_catalog_covers_html_placeholders_both_apps_and_all_supported_countries(self):
  import re,phonenumbers
  source=(c.WEB/'index.html').read_text();keys=re.findall(r'\{\{copy:([^}]+)\}\}|data-copy-(?:placeholder|aria-label|alt)="([^"]+)"',source)
  for mode,fields in c.definitions().items():
   self.assertTrue(all((a or b) in fields for a,b in keys))
   self.assertTrue(all('country.'+region in fields for region in phonenumbers.SUPPORTED_REGIONS))
   out=c.render_index(source,{key:item['text'] for key,item in fields.items()})
   self.assertNotIn('{{copy:',out);self.assertNotIn('data-copy-placeholder',out)
