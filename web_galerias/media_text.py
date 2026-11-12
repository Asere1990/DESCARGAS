"""Search associations: caption + Telegram replies + OCR, materialized per album.

Every visible media item keeps three independent indexed sources. The GIN-backed
wg_media.search_text contains their normalized UNION, so customer searches never
call Telegram, OCR, or Cloudflare R2. A low-priority outbox mirrors the source
mapping to private R2 JSON for future recovery, separate from media downloads.
"""
import asyncio
import json
import logging
import time
from collections import defaultdict
from io import BytesIO

from .core import normal

LOG = logging.getLogger("gallery-media-text")
SCHEMA = """
-- An old version declared wg_replies but did not populate it. Reuse that name.
CREATE TABLE IF NOT EXISTS wg_replies(
 folder BIGINT NOT NULL,
 parent_message BIGINT NOT NULL,
 reply_message BIGINT NOT NULL,
 words TEXT NOT NULL,
 PRIMARY KEY(folder,reply_message));
CREATE INDEX IF NOT EXISTS wg_replies_parent
 ON wg_replies(folder,parent_message);
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS search_caption TEXT NOT NULL DEFAULT '';
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS search_replies TEXT NOT NULL DEFAULT '';
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS search_ocr TEXT NOT NULL DEFAULT '';
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS ocr_text TEXT NOT NULL DEFAULT '';
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS metadata_rev BIGINT NOT NULL DEFAULT 0;
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS r2_metadata_rev BIGINT NOT NULL DEFAULT 0;
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS ocr_checked BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS ocr_next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE wg_media ADD COLUMN IF NOT EXISTS ocr_attempts INT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS wg_media_pending_ocr ON wg_media(id DESC)
 WHERE state IN ('preview','ready') AND thumb_key IS NOT NULL AND NOT ocr_checked;
CREATE INDEX IF NOT EXISTS wg_media_grouped_text
 ON wg_media(folder,grouped) WHERE grouped IS NOT NULL;
CREATE TABLE IF NOT EXISTS wg_media_text_dirty(
 folder BIGINT NOT NULL,
 message BIGINT NOT NULL,
 attempts INT NOT NULL DEFAULT 0,
 next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
 PRIMARY KEY(folder,message));
CREATE INDEX IF NOT EXISTS wg_media_text_dirty_due
 ON wg_media_text_dirty(next_attempt,folder,message);
ALTER TABLE wg_media_text_dirty ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_media_text_dirty FROM PUBLIC;
CREATE TABLE IF NOT EXISTS wg_media_text_outbox(
 media BIGINT PRIMARY KEY REFERENCES wg_media(id) ON DELETE CASCADE,
 revision BIGINT NOT NULL, attempts INT NOT NULL DEFAULT 0,
 next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS wg_media_text_outbox_due
 ON wg_media_text_outbox(next_attempt,media);
ALTER TABLE wg_media_text_outbox ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON wg_media_text_outbox FROM PUBLIC;
-- A bounded historical pass finds existing replies without moving primary scan
-- cursors, changing Telegram sessions, or forcing a re-download of multimedia.
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS reply_scan_cursor BIGINT NOT NULL DEFAULT 0;
ALTER TABLE wg_folders ADD COLUMN IF NOT EXISTS reply_scan_complete BOOLEAN NOT NULL DEFAULT FALSE;
"""


def _dedup_text(parts, *, limit=6500, per_part=1500):
    """Deduplicate identical captions/replies within a media group."""
    seen=set()
    result=[]
    used=0
    for value in parts:
        value=str(value or "").strip().replace("\x00","")
        if not value:
            continue
        value=value[:per_part]
        key=normal(value)
        if not key or key in seen:
            continue
        seen.add(key)
        available=limit-used
        if available<=0:
            break
        value=value[:available]
        result.append(value)
        used+=len(value)+1
    return "\n".join(result)


async def capture_reply(g, folder, m):
    """Persist only an explicit reply comment, never index free-form chat."""
    parent=getattr(getattr(m,"reply_to",None),"reply_to_msg_id",None)
    if not parent or not isinstance(parent,int) or parent<=0:
        return None
    if getattr(m,"noforwards",False) or getattr(m,"ttl_period",None) or (
        getattr(getattr(m,"media",None),"ttl_seconds",None)):
        return None
    text=str(getattr(m,"message","") or "").strip()
    if not text:
        return None
    text=text[:1500]
    await g.db().execute("""
       INSERT INTO wg_replies(folder,parent_message,reply_message,words)
       VALUES($1,$2,$3,$4)
       ON CONFLICT(folder,reply_message)
       DO UPDATE SET parent_message=excluded.parent_message,words=excluded.words
       WHERE wg_replies.parent_message IS DISTINCT FROM excluded.parent_message
          OR wg_replies.words IS DISTINCT FROM excluded.words
    """,folder,parent,m.id,text)
    return int(parent)


async def _ancestors(db,folder,anchors):
    """Follow 4 explicit reply edges to find a media anchor for nested comments."""
    current={int(x) for x in anchors if x and int(x)>0}
    all_ids=set(current)
    for _ in range(4):
        if not current:
            break
        rows=await db.fetch("""
           SELECT parent_message FROM wg_replies
           WHERE folder=$1 AND reply_message=ANY($2::bigint[])
        """,folder,list(current))
        next_ids={int(x["parent_message"]) for x in rows
                  if x.get("parent_message")}
        next_ids.difference_update(all_ids)
        if not next_ids:
            break
        all_ids.update(next_ids)
        current=next_ids
    return all_ids


async def _reply_tree(db,folder,member_ids):
    """Only comments linked directly or through at most 3 nested replies."""
    if not member_ids:
        return []
    rows=await db.fetch("""
       WITH RECURSIVE thread AS (
         SELECT r.reply_message,r.parent_message,r.words,
                1 AS depth, ARRAY[r.reply_message] AS path
         FROM wg_replies r
         WHERE r.folder=$1 AND r.parent_message=ANY($2::bigint[])
         UNION ALL
         SELECT r.reply_message,r.parent_message,r.words,
                t.depth+1,t.path||r.reply_message
         FROM wg_replies r
         JOIN thread t ON t.reply_message=r.parent_message
         WHERE r.folder=$1 AND t.depth<3
           AND NOT r.reply_message=ANY(t.path)
       )
       SELECT DISTINCT reply_message,words FROM thread
       ORDER BY reply_message ASC LIMIT 120
    """,folder,member_ids)
    return [x["words"] for x in rows]


async def rebuild(g, folder, changed_ids):
    """Refresh album-wide associations; all 10 album items match a reply once.

    Called only from catalog/backfill/OCR workers, never from customer searches.
    """
    identifiers={int(x) for x in changed_ids if x and int(x)>0}
    if not identifiers:
        return 0
    db=g.db()
    anchors=await _ancestors(db,folder,identifiers)
    if not anchors:
        return 0
    rows=await db.fetch("""
        WITH affected AS (
           SELECT DISTINCT grouped FROM wg_media
           WHERE folder=$1 AND message=ANY($2::bigint[])
             AND grouped IS NOT NULL
        )
        SELECT id,message,grouped,label,ocr_text
        FROM wg_media WHERE folder=$1 AND state<>'removed' AND
         (message=ANY($2::bigint[]) OR
          grouped IN (SELECT grouped FROM affected))
        ORDER BY id
    """,folder,list(anchors))
    groups=defaultdict(list)
    for row in rows:
        key="album:"+str(row["grouped"]) if row["grouped"] else "media:"+str(row["message"])
        groups[key].append(row)
    updated=0
    for group in groups.values():
        member_ids=[int(x["message"]) for x in group]
        caption=_dedup_text((x["label"] for x in group))
        replies=_dedup_text(await _reply_tree(db,folder,member_ids))
        ocr=_dedup_text((x["ocr_text"] for x in group))
        # The 3 sources are stored separately; GIN uses this precomputed value.
        indexed=normal("\n".join((caption,replies,ocr)))[:15000]
        ids=[int(x["id"]) for x in group]
        await db.execute("""
           WITH changed AS (
             UPDATE wg_media
             SET search_caption=$3,search_replies=$4,search_ocr=$5,
                 search_text=$6,metadata_rev=metadata_rev+1
             WHERE folder=$1 AND id=ANY($2::bigint[]) AND state<>'removed'
               AND (search_caption,search_replies,search_ocr,search_text,
                    metadata_rev=0) IS DISTINCT FROM
                   ($3::text,$4::text,$5::text,$6::text,FALSE)
             RETURNING id,metadata_rev
           )
           INSERT INTO wg_media_text_outbox(media,revision,next_attempt)
           SELECT id,metadata_rev,0 FROM changed
           ON CONFLICT(media) DO UPDATE SET revision=excluded.revision,
              attempts=0,next_attempt=0
        """,folder,ids,caption,replies,ocr,indexed)
        updated+=len(group)
    return updated


async def enqueue(g,folder,changed_ids):
    """Single small DB statement; thumbnails and users do not wait for rebuild."""
    ids=sorted({int(x) for x in changed_ids if x and int(x)>0})
    if not ids:
        return 0
    await g.db().execute("""
      INSERT INTO wg_media_text_dirty(folder,message,next_attempt)
      SELECT $1,ids.value,0
      FROM unnest($2::bigint[]) AS ids(value)
      ON CONFLICT(folder,message) DO UPDATE
      SET next_attempt=0,attempts=0
    """,int(folder),ids)
    return len(ids)


async def process_dirty(g,limit=35):
    """One bounded set of album/reply anchors, outside active sync loops."""
    db=g.db()
    now=time.time()
    rows=await db.fetch("""
      WITH chosen AS (
        SELECT folder,message FROM wg_media_text_dirty
        WHERE next_attempt<=$1
        ORDER BY next_attempt,folder,message LIMIT $2
        FOR UPDATE SKIP LOCKED
      )
      UPDATE wg_media_text_dirty d
      SET next_attempt=$1+90,attempts=d.attempts+1
      FROM chosen c WHERE c.folder=d.folder AND c.message=d.message
      RETURNING d.folder,d.message,d.attempts
    """,now,max(1,min(50,int(limit))))
    if not rows:
        return 0
    groups=defaultdict(list)
    for row in rows:
        groups[int(row["folder"])].append(int(row["message"]))
    for folder,ids in groups.items():
        try:
            await rebuild(g,folder,ids)
            await db.execute("""
              DELETE FROM wg_media_text_dirty
              WHERE folder=$1 AND message=ANY($2::bigint[])
            """,folder,ids)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Album text enrichment retry (%s)",type(exc).__name__)
            await db.execute("""
              UPDATE wg_media_text_dirty
              SET next_attempt=$3
              WHERE folder=$1 AND message=ANY($2::bigint[])
            """,folder,ids,time.time()+60)
    return len(rows)


async def backfill_unindexed(g,limit=30):
    """One bounded page; never blocks first paint or reparses all media at once."""
    db=g.db()
    rows=await db.fetch("""
       SELECT folder,message FROM wg_media
       WHERE state IN ('preview','ready') AND metadata_rev=0
       ORDER BY id ASC LIMIT $1
    """,max(1,min(50,int(limit))))
    if not rows:
        return 0
    by_folder=defaultdict(list)
    for item in rows:
        by_folder[int(item["folder"])].append(int(item["message"]))
    for folder,ids in by_folder.items():
        await rebuild(g,folder,ids)
    return len(rows)


def object_key(folder,message):
    folder=int(folder);message=int(message)
    if folder<=0 or message<=0:
        raise ValueError("Invalid source metadata identifiers")
    return f"folders/{folder}/{message}/search-metadata-v1.json"


def payload(row):
    """Stable, recoverable metadata, without Telegram auth or customer IDs."""
    return {
       "format":"web-galerias-search-v1",
       "folder":int(row["folder"]),"message":int(row["message"]),
       "album":str(row["grouped"]) if row["grouped"] else None,
       "revision":int(row["metadata_rev"]),
       "sources":{
           "caption":str(row["search_caption"]),
           "replies":str(row["search_replies"]),
           "ocr":str(row["search_ocr"]),
       },
       "local":{"caption":str(row["label"] or ""),
                "ocr":str(row["ocr_text"] or "")},
       "search_text":str(row["search_text"] or ""),
    }


async def archive_one(g):
    """Best-effort one JSON in R2, independently retried after restart."""
    db=g.db()
    now=time.time()
    row=await db.fetchrow("""
       WITH due AS (
        SELECT media FROM wg_media_text_outbox
        WHERE next_attempt<=$1 ORDER BY next_attempt,media
        LIMIT 1 FOR UPDATE SKIP LOCKED
       )
       UPDATE wg_media_text_outbox q
       SET attempts=attempts+1,next_attempt=$1+120
       FROM due WHERE q.media=due.media
       RETURNING q.media,q.revision,q.attempts
    """,now)
    if not row:
        return False
    media=await db.fetchrow("""
       SELECT id,folder,message,grouped,label,ocr_text,
              search_caption,search_replies,search_ocr,search_text,
              metadata_rev
       FROM wg_media WHERE id=$1 AND state<>'removed'
    """,row["media"])
    if not media:
        await db.execute("DELETE FROM wg_media_text_outbox WHERE media=$1",
                         row["media"])
        return True
    if int(media["metadata_rev"])!=int(row["revision"]):
        return True
    try:
        content=json.dumps(payload(media),ensure_ascii=False,
                           sort_keys=True,separators=(",",":")).encode("utf-8")
        if len(content)>60000:
            raise ValueError("Search metadata exceeds configured maximum")
        key=object_key(media["folder"],media["message"])
        def write():
            client=g.storage._client()
            client.put_object(Bucket=g.storage.bucket,Key=key,Body=content,
                              ContentType="application/json",
                              CacheControl="private,max-age=300")
        await asyncio.to_thread(write)
        await db.execute("""
          UPDATE wg_media SET r2_metadata_rev=$2
          WHERE id=$1 AND metadata_rev=$2
        """,media["id"],int(row["revision"]))
        await db.execute("""
          DELETE FROM wg_media_text_outbox WHERE media=$1 AND revision=$2
        """,media["id"],int(row["revision"]))
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        retries=int(row["attempts"])
        delay=min(3600,30*2**min(retries,6))
        LOG.warning("R2 search text backup deferred (%s)",type(exc).__name__)
        await db.execute("""
          UPDATE wg_media_text_outbox SET next_attempt=$3
          WHERE media=$1 AND revision=$2
        """,media["id"],int(row["revision"]),time.time()+delay)
        return False


async def restore_from_r2(g,folder,message):
    """Explicit recovery API, never used on normal searches or page loads."""
    key=object_key(folder,message)
    def read():
        response=g.storage._client().get_object(Bucket=g.storage.bucket,Key=key)
        body=response["Body"]
        try:
            content=body.read(65001)
        finally:
            body.close()
        if len(content)>65000:
            raise ValueError("R2 metadata too large")
        return json.loads(content)
    saved=await asyncio.to_thread(read)
    if (saved.get("format")!="web-galerias-search-v1"
        or saved.get("folder")!=int(folder) or saved.get("message")!=int(message)):
        raise ValueError("Search metadata identity mismatch")
    row=await g.db().fetchrow("""
       SELECT id,metadata_rev FROM wg_media WHERE folder=$1 AND message=$2
       AND state<>'removed'
    """,folder,message)
    if not row:
        return False
    old=saved.get("sources") or {}
    if not isinstance(old,dict):
        raise ValueError("Invalid archived sources")
    caption=str(old.get("caption") or "")[:6500]
    replies=str(old.get("replies") or "")[:6500]
    ocr=str(old.get("ocr") or "")[:6500]
    raw=normal("\n".join((caption,replies,ocr)))[:15000]
    await g.db().execute("""
       UPDATE wg_media SET search_caption=$2,search_replies=$3,
         search_ocr=$4,search_text=$5,
         ocr_text=$6,metadata_rev=metadata_rev+1
       WHERE id=$1 AND metadata_rev<=$7
    """,row["id"],caption,replies,ocr,raw,
       str((saved.get("local") or {}).get("ocr") or "")[:2500],
       max(int(saved.get("revision",0)),int(row["metadata_rev"])))
    return True


async def run(g):
    """Strictly third-priority enrichment, while client galleries stay fast."""
    await asyncio.sleep(15)
    cycle=0
    while not g.closed:
        try:
            if not g.storage.ready:
                await asyncio.sleep(45)
                continue
            # A slow initial migration is bounded and never part of startup.
            if cycle%12==0:
                await backfill_unindexed(g,limit=20)
            await process_dirty(g,limit=35)
            await archive_one(g)
            cycle+=1
            await asyncio.sleep(2.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Gallery search metadata retry (%s)",type(exc).__name__)
            await asyncio.sleep(20)
