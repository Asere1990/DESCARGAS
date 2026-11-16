"""End-to-end test of the dedicated bot intake, without real Telegram or R2."""
import asyncio
import io
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from web_galerias import bot_feed as feed


class FakeConnection:
    def __init__(self):
        self.sql = []
        self.inserted_media = None

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchval(self, query, *params):
        self.sql.append((query, params))
        if "SELECT 1 FROM wg_bot_feed_routes" in query:
            return 1
        if "nextval(" in query:
            return 14
        if "INSERT INTO wg_media(" in query:
            self.inserted_media = params
            return 998
        raise AssertionError("Unexpected SQL read: "+query[:150])

    async def fetchrow(self, query, *params):
        self.sql.append((query, params))
        if "wg_bot_feed_items" in query:
            return {"status":"processing", "media_id":None}
        raise AssertionError("Unexpected SQL row: "+query[:150])

    async def execute(self, query, *params):
        self.sql.append((query, params))


class FakePool:
    def __init__(self, conn, album_caption=""):
        self.conn = conn
        self.album_caption=album_caption
        self.calls=[]

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    async def fetchval(self, query, *params):
        self.calls.append((query,params))
        if "caption FROM wg_bot_feed_items" in query:
            return self.album_caption
        raise AssertionError("Unexpected pool read")


class FakeStorage:
    ready = True
    bucket = "private"
    def __init__(self, first_size, uploaded_size):
        self.object_size=AsyncMock(side_effect=[first_size,uploaded_size])
        self.exists=AsyncMock(return_value=False)
        self.upload_stream=AsyncMock()
        self.put_object=Mock(return_value={})

    def _client(self):
        return SimpleNamespace(put_object=self.put_object)


def jpeg_data():
    out=io.BytesIO()
    Image.new("RGB",(48,48),(78,64,120)).save(out,format="JPEG")
    return out.getvalue()


class BotToR2PipelineTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, photo=True, caption="Foto 1", grouped="album-3"):
        conn=FakeConnection()
        pool=FakePool(conn,album_caption="Mi álbum cubano")
        payload=jpeg_data()
        media_type="image/jpeg" if photo else "video/mp4"
        filename="foto.jpg" if photo else "clip.mp4"
        file_bytes=payload if photo else b"test-video"
        storage=FakeStorage(None,len(file_bytes))
        g=SimpleNamespace(db=lambda:pool,storage=storage)
        item={
            "folder":52,"group_id":-1009876543210,
            "message_id":23 if photo else 24,
            "revision":"valid-uuid-for-test",
            "file_size":len(file_bytes),
            "kind":"photo" if photo else "video",
            "source_type":"photo" if photo else "video",
            "mime":media_type,"filename":filename,
            "caption":caption,"grouped":grouped,
            "width":48,"height":48,
            "file_id":"file","thumb_file_id":"thumb",
            "message_date":1700000000.0
        }
        return g,conn,pool,storage,item,file_bytes

    async def test_forwarded_photo_is_downloaded_once_and_marked_ready(self):
        g,conn,pool,storage,item,content=self.fixture()
        async def fake_chunks(session,url,*,limit):
            yield content[:50]
            yield content[50:]
        with patch.object(feed,"file_info",new_callable=AsyncMock,
                          return_value=("https://telegram.example/file",len(content))
        ) as info,patch.object(feed,"file_chunks",new=fake_chunks),patch.object(
            feed,"route_current",new_callable=AsyncMock,return_value=True):
            await feed.process_item(g,None,"token",item)
        info.assert_awaited_once()
        self.assertIsNotNone(conn.inserted_media)
        self.assertEqual(conn.inserted_media[0],52)
        self.assertEqual(conn.inserted_media[2],"photo")
        self.assertEqual(conn.inserted_media[9],"Foto 1")
        self.assertIn("Mi álbum cubano",conn.inserted_media[10])
        self.assertIn("bot:1009876543210:album-3",conn.inserted_media[11])
        self.assertTrue(any("status='ready'" in sql for sql,_ in conn.sql))
        self.assertEqual(storage.upload_stream.await_count,0)
        self.assertEqual(storage.put_object.call_count,2)
        self.assertEqual(pool.calls[0][1],(-1009876543210,"album-3"))
        self.assertTrue(all("descarga_accounts" not in sql for sql,_ in conn.sql))

    async def test_video_uses_bounded_streaming_and_makes_thumbnail(self):
        g,conn,pool,storage,item,content=self.fixture(photo=False)
        async def fake_chunks(session,url,*,limit):
            yield content
        with patch.object(feed,"file_info",new_callable=AsyncMock,
                          return_value=("https://telegram.example/file",len(content))
        ),patch.object(feed,"route_current",new_callable=AsyncMock,
                       return_value=True),patch.object(
            feed,"image_preview",new_callable=AsyncMock,return_value=b"fakejpeg"):
            await feed.process_item(g,None,"token",item)
        storage.upload_stream.assert_awaited_once()
        args=storage.upload_stream.await_args.args
        self.assertEqual(args[1],"botfeed/1009876543210/24/original.mp4")
        self.assertEqual(storage.upload_stream.await_args.kwargs["expected_size"],len(content))
        self.assertEqual(storage.put_object.call_count,1)
        self.assertEqual(conn.inserted_media[2],"video")
        self.assertTrue(any("status='ready'" in sql for sql,_ in conn.sql))

    async def test_existing_r2_original_not_downloaded_again(self):
        g,conn,pool,storage,item,content=self.fixture()
        storage.object_size=AsyncMock(return_value=len(content))
        with patch.object(feed,"file_info",new_callable=AsyncMock,
                          return_value=("https://telegram.example/file",len(content))
        ),patch.object(feed,"file_chunks") as chunks,patch.object(
            feed,"route_current",new_callable=AsyncMock,return_value=True
        ),patch.object(feed,"image_preview",new_callable=AsyncMock,
                       return_value=b"preview"):
            await feed.process_item(g,None,"token",item)
        chunks.assert_not_called()
        storage.upload_stream.assert_not_awaited()
        self.assertEqual(storage.put_object.call_count,1)
        self.assertIsNotNone(conn.inserted_media)


if __name__=="__main__":
    unittest.main()
