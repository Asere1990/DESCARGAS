"""Video HTTP Range fallback: safety, streaming and iPhone seek probes."""
import io
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet
from web_galerias import downloads


class FakeR2:
    def __init__(self, data):
        self.data=data
        self.calls=[]
        self.max_read=0

    def head_object(self, **kwargs):
        self.calls.append(("head",kwargs))
        return {"ContentLength":len(self.data)}

    def get_object(self, **kwargs):
        self.calls.append(("get",kwargs))
        payload=self.data
        range_header=kwargs.get("Range")
        if range_header:
            match=re.fullmatch(r"bytes=(\d*)-(\d*)",range_header)
            assert match is not None
            left,right=match.groups()
            if left:
                begin=int(left)
                end=min(len(payload)-1,int(right)) if right else len(payload)-1
            else:
                begin=max(0,len(payload)-int(right))
                end=len(payload)-1
            if begin>=len(payload) or begin>end:
                from botocore.exceptions import ClientError
                raise ClientError({"Error":{"Code":"InvalidRange"}},"GetObject")
            content=payload[begin:end+1]
            content_range=f"bytes {begin}-{end}/{len(payload)}"
            status=206
        else:
            content=payload
            content_range=None
            status=200
        parent=self
        class Stream(io.BytesIO):
            def read(self,size=-1):
                parent.max_read=max(parent.max_read,size)
                return super().read(size)
        result={"Body":Stream(content),"ContentLength":len(content),
                "ResponseMetadata":{"HTTPStatusCode":status}}
        if content_range:result["ContentRange"]=content_range
        return result


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.body=b"0123456789"*1024
        self.r2=FakeR2(self.body)
        self.allowed=True
        self.database=SimpleNamespace(fetchrow=AsyncMock(
            return_value={"folder":10,"thumb_key":"folders/10/1/thumb.jpg",
                          "file_key":"folders/10/1/original.mp4",
                          "kind":"video","mime":"video/mp4"}))
        self.storage=SimpleNamespace(
            ready=True,bucket="private",
            safe_key=lambda key:key,
            _client=lambda:self.r2,
            signed_get=lambda key,duration=180:"https://example.invalid/private",
            object_size=AsyncMock(return_value=len(self.body)))
        async def permission(uid,folder):
            return self.allowed
        self.gallery=SimpleNamespace(
            db=lambda:self.database,permitted=permission,
            storage=self.storage,s=SimpleNamespace(cipher=Fernet(Fernet.generate_key())))
        app=web.Application()
        app.router.add_get("/media/{item}/{kind}",lambda req:downloads.media(self.gallery,req))
        self.client=TestClient(TestServer(app))
        await self.client.start_server()
        token=downloads.temporary_link(self.gallery,54321,55,"view")
        self.url="/media/55/view?t="+token

    async def asyncTearDown(self):
        await self.client.close()

    async def test_small_initial_range_and_seek_response(self):
        for header,expected,length in [
                ("bytes=0-1","bytes 0-1/10240",2),
                ("bytes=100-199","bytes 100-199/10240",100),
                ("bytes=-10","bytes 10230-10239/10240",10)]:
            async with self.client.get(self.url+"&stream=1",headers={"Range":header}) as response:
                self.assertEqual(response.status,206)
                self.assertEqual(response.headers["Content-Range"],expected)
                self.assertEqual(response.headers["Accept-Ranges"],"bytes")
                self.assertEqual(int(response.headers["Content-Length"]),length)
                self.assertEqual(len(await response.read()),length)
        self.assertLessEqual(self.r2.max_read,512*1024)

    async def test_no_range_is_chunked_without_buffering_whole_video(self):
        async with self.client.get(self.url+"&stream=1") as response:
            self.assertEqual(response.status,200)
            self.assertEqual(await response.read(),self.body)

    async def test_head_does_not_open_video_stream(self):
        async with self.client.head(self.url+"&stream=1") as response:
            self.assertEqual(response.status,200)
            self.assertEqual(int(response.headers["Content-Length"]),len(self.body))
            self.assertEqual(await response.read(),b"")
        self.assertTrue(all(kind=="head" for kind,_ in self.r2.calls))

    async def test_ordinary_video_and_thumbnail_keep_direct_r2(self):
        async with self.client.get(self.url,allow_redirects=False) as response:
            self.assertEqual(response.status,302)
            self.assertEqual(response.headers["Location"],"https://example.invalid/private")
        thumb=downloads.temporary_link(self.gallery,54321,55,"thumb")
        async with self.client.get("/media/55/thumb?t="+thumb+"&stream=1",
                                   allow_redirects=False) as response:
            self.assertEqual(response.status,302)
        self.assertEqual(self.r2.calls,[])

    async def test_permission_and_token_required_before_stream(self):
        self.allowed=False
        async with self.client.get(self.url+"&stream=1") as response:
            self.assertEqual(response.status,403)
        self.allowed=True
        async with self.client.get("/media/55/view?t=bad&stream=1") as response:
            self.assertEqual(response.status,403)
        self.assertEqual(self.r2.calls,[])

    async def test_rejects_multi_range_and_out_of_bounds(self):
        async with self.client.get(self.url+"&stream=1",
                                   headers={"Range":"bytes=0-1,8-9"}) as response:
            self.assertEqual(response.status,416)
        self.assertEqual(self.r2.calls,[])
        async with self.client.get(self.url+"&stream=1",
                                   headers={"Range":"bytes=99999-"}) as response:
            self.assertEqual(response.status,416)
            self.assertEqual(response.headers["Content-Range"],"bytes */10240")


if __name__=="__main__":
    unittest.main()
