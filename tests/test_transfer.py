import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from descargas_app.transfer import prepare_album


class TransferTests(unittest.IsolatedAsyncioTestCase):
    def items(self,n):
        return [SimpleNamespace(id=i,file=SimpleNamespace(ext='.mp4')) for i in range(n)]

    async def test_overlap_preserves_order_and_bytes(self):
        active=0;peak=0;released=asyncio.Event()
        class Source:
            async def download_media(_,item,file,progress_callback):
                nonlocal active,peak
                active+=1;peak=max(peak,active)
                if active==2:released.set()
                await asyncio.wait_for(released.wait(),1)
                await progress_callback(4,4)
                Path(file).write_bytes(b'abcd')
                active-=1
                return file
        async def prepare(path,item):
            self.assertEqual(path.read_bytes(),b'abcd')
            return item.id
        with tempfile.TemporaryDirectory() as directory:
            result=await prepare_album(Source(),self.items(5),directory,prepare,10,50)
        self.assertEqual(result,list(range(5)))
        self.assertEqual(peak,2)

    async def test_failure_cancels_siblings_before_return(self):
        cancelled=asyncio.Event();started=asyncio.Event()
        class Source:
            async def download_media(_,item,file,progress_callback):
                if item.id==0:
                    await started.wait()
                    raise ValueError('El archivo no se descargó completo.')
                started.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
        async def prepare(*args):raise AssertionError('Must not upload incomplete files')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                await prepare_album(Source(),self.items(2),directory,prepare,10,50)
        self.assertTrue(cancelled.is_set())

    async def test_album_limit_includes_concurrent_files(self):
        barrier=asyncio.Event()
        class Source:
            async def download_media(_,item,file,progress_callback):
                await progress_callback(6,6)
                barrier.set()
                await asyncio.sleep(0)
                Path(file).write_bytes(b'123456')
                return file
        async def prepare(*args):return 'uploaded'
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                await prepare_album(Source(),self.items(2),directory,prepare,10,10)
