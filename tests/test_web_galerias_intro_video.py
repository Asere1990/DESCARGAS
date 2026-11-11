"""Full-screen Gallery video: iPhone MOV conversion, private R2 and confirmation."""
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch
from aiohttp import web
from web_galerias import intro_video
from web_galerias.admin import cleaned_design
from web_galerias.core import DEFAULTS

ROOT=Path(__file__).resolve().parents[1]/"web_galerias"


class Content:
    def __init__(self,data):self.data=data
    async def iter_chunked(self,_):
        for i in range(0,len(self.data),70000):
            yield self.data[i:i+70000]


class IntroVideoTests(unittest.IsolatedAsyncioTestCase):
    def build(self,content=b"\x00\x00\x00\x18ftypqt  "+b"a"*160):
        db=SimpleNamespace(execute=AsyncMock())
        written={}
        async def put(path,key,mime):
            written[key]=Path(path).read_bytes()
        async def stat(key):return len(written[key])
        storage=SimpleNamespace(ready=True,upload=AsyncMock(side_effect=put),
                                object_size=AsyncMock(side_effect=stat),
                                bucket="private",safe_key=lambda k:k)
        g=SimpleNamespace(auth=AsyncMock(return_value=7539025033),db=lambda:db,
                          storage=storage,log=AsyncMock())
        request=SimpleNamespace(headers={"X-Intro-Mime":"video/quicktime"},
            content_length=len(content),content=Content(content))
        return g,db,storage,request,written

    async def test_valid_mov_upload_transcodes_and_verifies_before_publish(self):
        g,db,storage,request,written=self.build()
        fake_mp4=b"\x00\x00\x00\x18ftypisom"+b"b"*100
        def convert(_,output):Path(output).write_bytes(fake_mp4)
        with patch("web_galerias.permissions.is_admin",return_value=True):
            with patch.object(intro_video,"transcode_intro",side_effect=convert) as transcode:
                result=await intro_video.upload(g,request)
        self.assertTrue(result["complete"])
        self.assertEqual(result["video"],"mp4")
        transcode.assert_called_once()
        self.assertEqual(len(written),1)
        key=next(iter(written))
        self.assertTrue(key.startswith("web-galerias/presentation/"))
        self.assertEqual(written[key],fake_mp4)
        storage.object_size.assert_awaited_once_with(key)
        sql,payload=db.execute.await_args.args
        self.assertIn("VALUES('intro_promo'",sql)
        metadata=json.loads(payload)
        self.assertEqual(metadata["mime"],"video/mp4")
        self.assertEqual(metadata["key"],key)
        self.assertEqual(metadata["size"],len(fake_mp4))
        g.log.assert_awaited_once_with(7539025033,"admin-intro-video")

    async def test_cannot_upload_without_admin_permission(self):
        g,db,storage,request,written=self.build()
        with patch("web_galerias.permissions.is_admin",return_value=False):
            with self.assertRaises(web.HTTPForbidden):
                await intro_video.upload(g,request)
        storage.upload.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_huge_upload_and_invalid_format_are_rejected_early(self):
        g,db,storage,request,written=self.build()
        request.content_length=intro_video.MAX_INTRO_UPLOAD+1
        with patch("web_galerias.permissions.is_admin",return_value=True):
            with self.assertRaises(web.HTTPRequestEntityTooLarge):
                await intro_video.upload(g,request)
        request.content_length=160
        request.headers["X-Intro-Mime"]="application/octet-stream"
        with patch("web_galerias.permissions.is_admin",return_value=True):
            with self.assertRaises(web.HTTPBadRequest):
                await intro_video.upload(g,request)
        db.execute.assert_not_awaited()

    async def test_bad_conversion_keeps_previous_promo_intact(self):
        g,db,storage,request,written=self.build()
        def fail(_,__):raise web.HTTPUnprocessableEntity(text="Video incompatible")
        with patch("web_galerias.permissions.is_admin",return_value=True):
            with patch.object(intro_video,"transcode_intro",side_effect=fail):
                with self.assertRaises(web.HTTPUnprocessableEntity):
                    await intro_video.upload(g,request)
        db.execute.assert_not_awaited()
        storage.upload.assert_not_awaited()

    async def test_same_origin_player_supports_ranges_and_does_not_redirect(self):
        sample=b"\x00\x00\x00\x18ftypisom"+b"a"*150
        body=io.BytesIO(sample[:30])
        client=SimpleNamespace(get_object=MagicMock(return_value={
            "Body":body,"ContentLength":30,"ContentRange":"bytes 0-29/162"}))
        storage=SimpleNamespace(safe_key=lambda k:k,_client=lambda:client,bucket="bucket")
        g=SimpleNamespace(storage=storage)
        request=SimpleNamespace(headers={"Range":"bytes=0-29"})
        metadata={"key":"web-galerias/presentation/abc.mp4","mime":"video/mp4",
                  "size":len(sample)}
        with patch.object(web.StreamResponse,"prepare",new_callable=AsyncMock):
            with patch.object(web.StreamResponse,"write",new_callable=AsyncMock) as write:
                with patch.object(web.StreamResponse,"write_eof",new_callable=AsyncMock):
                    result=await intro_video.stream(g,request,metadata)
        self.assertEqual(result.status,206)
        self.assertEqual(result.content_type,"video/mp4")
        self.assertEqual(result.headers["Accept-Ranges"],"bytes")
        self.assertEqual(result.headers["Content-Range"],"bytes 0-29/162")
        self.assertEqual(client.get_object.call_args.kwargs["Range"],"bytes=0-29")
        self.assertGreaterEqual(write.await_count,1)
        self.assertTrue(body.closed)
        for invalid in ("bytes=0-2,5-7","bytes=999-1000","bytes=-0"):
            with self.subTest(value=invalid):
                with self.assertRaises(web.HTTPRequestRangeNotSatisfiable):
                    await intro_video.stream(g,SimpleNamespace(headers={"Range":invalid}),
                                             metadata)

    def test_mov_transcodes_to_browser_compatible_h264_mp4(self):
        from imageio_ffmpeg import get_ffmpeg_exe
        exe=get_ffmpeg_exe()
        with tempfile.TemporaryDirectory(prefix="test-gallery-video-") as dirname:
            original=Path(dirname)/"original.mov"
            output=Path(dirname)/"converted.mp4"
            subprocess.run([exe,"-nostdin","-hide_banner","-loglevel","error","-y",
                 "-f","lavfi","-i","color=c=blue:s=320x180:r=10",
                 "-t","0.5","-c:v","mpeg4",str(original)],
                 check=True,timeout=30)
            intro_video.transcode_intro(original,output)
            self.assertGreater(output.stat().st_size,100)
            self.assertIn(b"ftyp",output.read_bytes()[:64])
            self.assertLessEqual(output.stat().st_size,intro_video.MAX_INTRO_OUTPUT)

    def test_editor_has_confirmation_fullscreen_and_separate_size_note(self):
        html=(ROOT/"index.html").read_text(encoding="utf-8")
        js=(ROOT/"app.js").read_text(encoding="utf-8")
        css=(ROOT/"style.css").read_text(encoding="utf-8")
        app=(ROOT/"app.py").read_text(encoding="utf-8")
        admin=(ROOT/"admin.py").read_text(encoding="utf-8")
        for key in ('id="confirm-editor"','id="edit-intro_fit"','id="intro-sound"',
                    "video hasta 80 MB","autoplay muted loop playsinline"):
            self.assertIn(key,html)
        self.assertIn('$("confirm-editor").onclick=confirmEditorChanges',js)
        self.assertIn('$("upload-"+key).onchange=editorPendingFilesMessage',js)
        self.assertNotIn('onchange=e=>uploadAsset(key,e.target.files[0])',js)
        self.assertIn("new XMLHttpRequest()",js)
        self.assertIn('xhr.upload.onprogress',js)
        self.assertIn("const video=$(\"intro-video\"),placeholder",js)
        self.assertIn('intro.classList.add("has-intro-video")',js)
        self.assertIn("#intro.has-intro-video .intro-video-wrap video",css)
        self.assertIn("object-fit:cover",css)
        self.assertIn('app.router.add_post("/web-galerias/admin/intro-video"',app)
        self.assertIn('if key=="intro":',admin)
        self.assertIn('getattr',str(getattr(intro_video,"MAX_INTRO_UPLOAD",None)) if False else
                      "MAX_INTRO_UPLOAD=80*1024*1024")
        self.assertEqual(DEFAULTS["intro_fit"],"cover")
        self.assertEqual(cleaned_design({"intro_fit":"contain"})["intro_fit"],"contain")
        with self.assertRaises(web.HTTPBadRequest):
            cleaned_design({"intro_fit":"stretch"})


if __name__=="__main__":unittest.main()
