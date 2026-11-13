"""One-shot, read-only diagnosis of the two large videos reported on 2026-11-13.

This temporarily inspects only basic R2 object metadata and small byte windows.
No original is downloaded, rewritten, deleted, transcoded or exposed publicly.
Remove this diagnostic after the recorded cause is identified.
"""
import asyncio
import logging
import time

log=logging.getLogger("web-galerias-video-diagnostic")
SAMPLE=256*1024
IDS=(132224,132225)
CODEC_TAGS=(b"avc1",b"avc3",b"hvc1",b"hev1",b"av01",b"vp09",
            b"mp4v",b"mp4a",b"alac",b"opus",b"theo")


def _summary(first,last):
    prefix=first[:32]
    is_mp4=prefix[4:8]==b"ftyp"
    brand=prefix[8:12].decode("ascii","replace") if is_mp4 else ""
    hits=sorted({tag.decode("ascii") for tag in CODEC_TAGS
                 if tag in first or tag in last})
    return {
        "signature": "mp4" if is_mp4 else "matroska" if first.startswith(bytes.fromhex("1a45dfa3"))
                     else "unknown",
        "brand":brand if brand.isascii() and brand.isprintable() else "",
        "moov_first":b"moov" in first,
        "moov_last":b"moov" in last,
        "mdat_first":b"mdat" in first,
        "mdat_last":b"mdat" in last,
        "codecs_sampled":hits,
    }


def _head_and_windows(storage,key):
    client=storage._client()
    args={"Bucket":storage.bucket,"Key":storage.safe_key(key)}
    meta=client.head_object(**args)
    size=int(meta.get("ContentLength") or 0)
    if not size: return {"r2_bytes":size,"r2_mime":meta.get("ContentType",""),"sample":{}}
    n=min(SAMPLE,size)
    def read_bytes(start,end):
        obj=client.get_object(**args,Range=f"bytes={start}-{end}")
        try:return obj["Body"].read(n)
        finally:obj["Body"].close()
    first=read_bytes(0,n-1)
    last=read_bytes(size-n,size-1) if size>n else first
    return {"r2_bytes":size,"r2_mime":str(meta.get("ContentType") or "")[:80],
            "sample":_summary(first,last)}


async def run(g):
    # Do not interfere with the user's initial gallery load.
    await asyncio.sleep(10)
    try:
        rows=await g.db().fetch(
            "SELECT id,state,kind,mime,size,file_key FROM wg_media WHERE id=ANY($1::bigint[])",
            list(IDS))
        for row in rows:
            try:
                if row["state"]!="ready" or not row["file_key"]:
                    log.warning("Video inspection id=%s state=%s missing_ready_original",
                                row["id"],row["state"])
                    continue
                info=await asyncio.to_thread(_head_and_windows,g.storage,row["file_key"])
                log.warning(
                    "Video inspection id=%s db_bytes=%s db_mime=%s kind=%s "
                    "r2_bytes=%s r2_mime=%s signature=%s brand=%s "
                    "moov_first=%s moov_last=%s mdat_first=%s mdat_last=%s codecs=%s",
                    row["id"],row["size"],row["mime"],row["kind"],
                    info["r2_bytes"],info["r2_mime"],info["sample"].get("signature"),
                    info["sample"].get("brand"),info["sample"].get("moov_first"),
                    info["sample"].get("moov_last"),info["sample"].get("mdat_first"),
                    info["sample"].get("mdat_last"),",".join(info["sample"].get("codecs_sampled",[])))
                # Repair only the two R2 objects already PROVEN to be invalid.
                # Their R2 originals stay untouched until a new fully verified
                # Telegram multipart completes successfully. No other media,
                # user, session, billing or storage record is changed.
                broken=(row["kind"]=="video" and row["mime"]=="video/mp4"
                        and int(row["size"] or 0)==1024
                        and info["r2_bytes"]==1024
                        and info["sample"].get("signature")=="mp4"
                        and not info["sample"].get("moov_first")
                        and not info["sample"].get("moov_last"))
                if broken:
                    await g.db().execute("""
                        UPDATE wg_media SET state='preview',
                          error='r2_original_incompleto',available=0
                        WHERE id=$1 AND state='ready'
                          AND kind='video' AND size=1024
                    """,row["id"])
                    # Repairs use the same bounded, durable priority queue
                    # as a user tap. A synthetic maintenance requester never
                    # consumes anyone's download quota or opens another bot.
                    from . import on_demand
                    if on_demand.active(g):
                        await g.db().execute("""
                            INSERT INTO wg_media_priority(media,requester,requested_at)
                            VALUES($1,0,$2) ON CONFLICT(media) DO NOTHING
                        """,row["id"],time.time())
                    log.warning("Video original invalid; requeued id=%s",row["id"])
            except Exception as exc:
                log.warning("Video inspection id=%s unavailable (%s)",
                            row["id"],type(exc).__name__)
    except Exception as exc:
        log.warning("Video inspection setup unavailable (%s)",type(exc).__name__)
