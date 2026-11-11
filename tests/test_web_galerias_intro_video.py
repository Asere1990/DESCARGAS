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
        fake_poster=b"\xff\xd8\xff"+b"first frame"
        def convert(_,output):Path(output).write_bytes(fake_mp4)
        def first_frame(_,output):Path(output).write_bytes(fake_poster)
        with patch("web_galerias.permissions.is_admin",return_value=True):
            with patch.object(intro_video,"transcode_intro",side_effect=convert) as transcode:
                with patch.object(intro_video,"extract_poster",side_effect=first_frame) as thumbnail:
                    result=await intro_video.upload(g,request)
        self.assertTrue(result["complete"])
        self.assertEqual(result["video"],"mp4")
        transcode.assert_called_once();thumbnail.assert_called_once()
        self.assertEqual(len(written),2)
        key=next(x for x in written if x.endswith(".mp4"))
        poster_key=next(x for x in written if x.endswith(".jpg"))
        self.assertTrue(key.startswith("web-galerias/presentation/"))
        self.assertEqual(written[key],fake_mp4)
        self.assertEqual(written[poster_key],fake_poster)
        self.assertEqual(storage.object_size.await_count,2)
        sql,payload=db.execute.await_args.args
        self.assertIn("VALUES('intro_promo'",sql)
        metadata=json.loads(payload)
        self.assertEqual(metadata["mime"],"video/mp4")
        self.assertEqual(metadata["key"],key)
        self.assertEqual(metadata["size"],len(fake_mp4))
        self.assertEqual(metadata["poster_key"],poster_key)
        self.assertEqual(metadata["poster_size"],len(fake_poster))
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

    async def test_low_memory_blocks_processing_without_touching_r2(self):
        g,db,storage,request,written=self.build()
        with patch("web_galerias.permissions.is_admin",return_value=True):
            with patch.object(intro_video,"available_memory_bytes",return_value=250*1024*1024):
                with self.assertRaises(web.HTTPServiceUnavailable) as error:
                    await intro_video.upload(g,request)
        self.assertIn("reservar memoria",error.exception.text)
        storage.upload.assert_not_awaited()
        db.execute.assert_not_awaited()

    async def test_memory_check_runs_again_after_upload_before_transcode(self):
        g,db,storage,request,written=self.build()
        with patch("web_galerias.permissions.is_admin",return_value=True):
            with patch.object(intro_video,"available_memory_bytes",
                              side_effect=[2*1024*1024*1024,300*1024*1024]):
                with patch.object(intro_video,"transcode_intro") as convert:
                    with self.assertRaises(web.HTTPServiceUnavailable):
                        await intro_video.upload(g,request)
        convert.assert_not_called()
        storage.upload.assert_not_awaited()
        db.execute.assert_not_awaited()

    def test_ffmpeg_decoder_filters_encoder_are_all_bounded(self):
        with tempfile.TemporaryDirectory(prefix="test-bounded-video-") as dirname:
            source=Path(dirname)/"upload.mov"
            target=Path(dirname)/"output.mp4"
            source.write_bytes(b"placeholder")
            def fake_process(command,**options):
                self.assertIn("1",command)
                self.assertEqual(command.count("-threads"),2)
                self.assertIn("-filter_threads",command)
                self.assertIn("-max_muxing_queue_size",command)
                self.assertLessEqual(int(command[command.index("-max_muxing_queue_size")+1]),256)
                target.write_bytes(b"\\x00\\x00\\x00\\x18ftypisom"+b"payload")
                return SimpleNamespace(returncode=0)
            with patch.object(intro_video.subprocess,"run",side_effect=fake_process):
                intro_video.transcode_intro(source,target)
            self.assertTrue(target.exists())

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

    def test_real_video_first_frame_is_prepared_as_small_jpeg(self):
        from PIL import Image
        from imageio_ffmpeg import get_ffmpeg_exe
        with tempfile.TemporaryDirectory(prefix="test-intro-poster-") as dirname:
            video=Path(dirname)/"clip.mp4"
            poster=Path(dirname)/"first-frame.jpg"
            subprocess.run([get_ffmpeg_exe(),"-nostdin","-hide_banner",
                "-loglevel","error","-y","-f","lavfi",
                "-i","color=c=blue:s=640x360:r=10",
                "-t","0.5","-c:v","libx264","-threads","1",
                "-pix_fmt","yuv420p","-movflags","+faststart",str(video)],
                timeout=35,check=True)
            intro_video.extract_poster(video,poster)
            self.assertLess(poster.stat().st_size,1024*1024)
            with Image.open(poster) as result:
                self.assertEqual(result.format,"JPEG")
                self.assertLessEqual(max(result.size),960)

    async def test_first_frame_endpoint_serves_photo_choice_without_video_download(self):
        from PIL import Image
        jpeg=io.BytesIO()
        Image.new("RGB",(100,60),(20,80,130)).save(jpeg,"JPEG")
        db=SimpleNamespace(
            fetchval=AsyncMock(return_value={"intro_media":"photo"}),
            fetchrow=AsyncMock(return_value={
                "mime":"image/jpeg","body":jpeg.getvalue()}))
        storage=SimpleNamespace(_client=MagicMock())
        g=SimpleNamespace(db=lambda:db,storage=storage)
        response=await intro_video.poster_asset(g,SimpleNamespace())
        self.assertEqual(response.content_type,"image/jpeg")
        self.assertEqual(response.body,jpeg.getvalue())
        db.fetchrow.assert_awaited_once()
        storage._client.assert_not_called()

    async def test_cached_r2_poster_uses_tiny_verified_object(self):
        jpeg=b"\xff\xd8\xff"+b"cover"
        stream=io.BytesIO(jpeg)
        client=SimpleNamespace(get_object=MagicMock(return_value={"Body":stream}))
        db=SimpleNamespace(fetchval=AsyncMock(side_effect=[
            {"intro_media":"video"},
            {"mime":"video/mp4","key":"web-galerias/presentation/x.mp4",
             "poster_key":"web-galerias/presentation/x.jpg",
             "poster_size":len(jpeg)}]))
        store=SimpleNamespace(_client=lambda:client,bucket="private",
                              safe_key=lambda k:k)
        response=await intro_video.poster_asset(
            SimpleNamespace(db=lambda:db,storage=store),SimpleNamespace())
        self.assertEqual(response.body,jpeg)
        self.assertEqual(response.content_type,"image/jpeg")
        self.assertEqual(client.get_object.call_args.kwargs["Key"],
                         "web-galerias/presentation/x.jpg")
        self.assertTrue(stream.closed)

    async def test_legacy_video_gets_poster_without_replacing_original(self):
        jpeg=b"\xff\xd8\xff"+b"first frame"
        meta={"key":"web-galerias/presentation/old.mp4",
              "mime":"video/mp4","size":100,"updated":2}
        db=SimpleNamespace(
            fetchval=AsyncMock(return_value=meta),
            execute=AsyncMock())
        async def upload(path,key,mime):pass
        store=SimpleNamespace(ready=True,safe_key=lambda k:k,upload=AsyncMock(side_effect=upload),
                              object_size=AsyncMock(return_value=len(jpeg)))
        g=SimpleNamespace(closed=False,storage=store,db=lambda:db)
        def fetch(_,__,path,___):Path(path).write_bytes(b"legacy mp4")
        def extract(_,path):Path(path).write_bytes(jpeg)
        with patch.object(intro_video.asyncio,"sleep",new_callable=AsyncMock):
            with patch.object(intro_video,"fetch_video_to_disk",side_effect=fetch):
                with patch.object(intro_video,"extract_poster",side_effect=extract):
                    with patch.object(intro_video,"require_video_memory"):
                        await intro_video.backfill_poster(g)
        self.assertEqual(store.upload.await_count,1)
        args=db.execute.await_args.args
        self.assertIn("UPDATE wg_config",args[0])
        self.assertEqual(args[1],meta["key"])
        new=json.loads(args[2])
        self.assertEqual(new["poster_size"],len(jpeg))
        self.assertEqual(new["key"],meta["key"])

    def test_intro_photo_video_preload_is_present_before_scripts(self):
        html=(ROOT/"index.html").read_text(encoding="utf-8")
        js=(ROOT/"app.js").read_text(encoding="utf-8")
        css=(ROOT/"style.css").read_text(encoding="utf-8")
        self.assertIn('rel="preload" as="image" fetchpriority="high"',html)
        self.assertLess(html.index('rel="preload"'),html.index('telegram-web-app.js'))
        self.assertIn('id="intro-hero-poster"',html)
        self.assertIn('src="/web-galerias/assets/intro-poster"',html)
        self.assertIn('id="edit-intro_media"',html)
        self.assertIn('id="upload-intro-photo"',html)
        self.assertIn('video.onplaying=()=>',js)
        self.assertIn('video.classList.add("intro-playing")',js)
        self.assertIn('poster.onerror=()=>',js)
        self.assertIn('intro_media',''.join(js.split("function populateEditor",1)[1].split("async function saveDesign",1)[0]))
        self.assertIn('api("admin-design",{design})',js)
        self.assertIn('#intro.has-intro-video .intro-video-wrap .intro-hero-poster',css)
        self.assertIn('#intro.has-intro-video .intro-video-wrap video.intro-playing',css)

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
        self.assertEqual(intro_video.MAX_INTRO_UPLOAD,80*1024*1024)
        self.assertEqual(DEFAULTS["intro_fit"],"cover")
        self.assertEqual(cleaned_design({"intro_fit":"contain"})["intro_fit"],"contain")
        with self.assertRaises(web.HTTPBadRequest):
            cleaned_design({"intro_fit":"stretch"})


if __name__=="__main__":unittest.main()
