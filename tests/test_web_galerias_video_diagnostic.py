"""Read-only diagnostic never mutates or transfers the full media."""
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch
from web_galerias import video_diagnostic as d


class R2:
    def __init__(self,data):
        self.data=data
        self.requests=[]
    def head_object(self,**kwargs):
        self.requests.append(("head",kwargs))
        return {"ContentLength":len(self.data),"ContentType":"video/mp4"}
    def get_object(self,**kwargs):
        import io
        self.requests.append(("get",kwargs))
        value=kwargs["Range"].removeprefix("bytes=")
        left,right=map(int,value.split("-"))
        return {"Body":io.BytesIO(self.data[left:right+1])}


class Check(unittest.IsolatedAsyncioTestCase):
    def test_reads_only_small_windows(self):
        data=(bytes.fromhex("000000186674797069736f6d")+b"mdat"+b"0"*(1024*1024)
              +b"moov"+b"avc1")
        obj=R2(data)
        s=N(_client=lambda:obj,bucket="private",safe_key=lambda key:key)
        r=d._head_and_windows(s,"folders/10/50/original.mp4")
        self.assertEqual(r["r2_bytes"],len(data))
        self.assertEqual(r["sample"]["signature"],"mp4")
        self.assertTrue(r["sample"]["moov_last"])
        self.assertTrue(all(op in ("head","get") for op,_ in obj.requests))
        self.assertEqual(len(obj.requests),3)
        for name,params in obj.requests:
            if name=="get":
                a,b=map(int,params["Range"][6:].split("-"))
                self.assertLessEqual(b-a+1,d.SAMPLE)

    async def test_logs_two_ids_only_and_catches_R2_errors(self):
        db=N(fetch=AsyncMock(return_value=[]))
        g=N(db=lambda:db)
        with patch.object(d.asyncio,"sleep",new=AsyncMock()):
            await d.run(g)
        db.fetch.assert_awaited_once()
        self.assertEqual(d.IDS,(132224,132225))


if __name__=="__main__":
    unittest.main()
