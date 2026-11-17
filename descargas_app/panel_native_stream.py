"""Verified, read-only HTTP streaming of purchased Stars attachments.

The original panel silently treated an early Telegram iterator EOF as a complete
HTTP 200, although Content-Length still declared the full multi-gigabyte video.
This helper verifies bytes written, resumes short Telegram reads from the last
byte, and reports client disconnects without logging URLs, IDs or credentials.
Only native-stars-file uses this path; normal panel media is unchanged.
"""
import asyncio
import logging
import time

log = logging.getLogger("descargas.stars.native")
CHUNK_SIZE = 256 * 1024
MT_PROTO_ALIGNMENT = 4096
MAX_EARLY_EOF_RETRIES = 4


async def write_verified_range(response, client, media, start, end, check=None):
    """Write exactly the inclusive HTTP byte range or fail without write_eof.

    A completed aiohttp response after fewer bytes than Content-Length is a
    false success to a native browser download manager; never do that.
    """
    start, end = int(start), int(end)
    if start < 0 or end < start:
        raise ValueError("Intervalo de descarga incorrecto.")
    expected = end - start + 1
    sent = 0
    restarts = 0
    checked = 0.0
    try:
        while sent < expected:
            offset = start + sent
            aligned = offset - offset % MT_PROTO_ALIGNMENT
            position = aligned
            before = sent
            iterator = client.iter_download(
                media, offset=aligned,
                request_size=CHUNK_SIZE, chunk_size=CHUNK_SIZE)
            try:
                async for chunk in iterator:
                    if check is not None and time.monotonic() - checked > 5:
                        await check()
                        checked = time.monotonic()
                    left = max(0, start + sent - position)
                    right = min(len(chunk), end - position + 1)
                    if right > left:
                        body = chunk[left:right]
                        await response.write(body)
                        sent += len(body)
                    position += len(chunk)
                    if sent >= expected:
                        break
            finally:
                await iterator.close()
            if sent >= expected:
                break
            restarts += 1
            log.warning("Native Stars Telegram source stopped early: delivered=%s expected=%s retry=%s",
                        sent, expected, restarts)
            if restarts > MAX_EARLY_EOF_RETRIES:
                raise ConnectionError("Incomplete Telegram media while streaming; refused early HTTP success.")
            # A short RPC download is not a completed response. Resume from
            # the exact byte offset instead of starting the video at zero.
            await asyncio.sleep(min(2.0, .3 * restarts))
        await response.write_eof()
        log.info("Native Stars HTTP transfer completed: delivered=%s expected=%s",
                 sent, expected)
        return response
    except (ConnectionResetError, BrokenPipeError):
        log.warning("Native Stars client disconnected: delivered=%s expected=%s",
                    sent, expected)
        raise
    except asyncio.CancelledError:
        log.info("Native Stars transfer cancelled: delivered=%s expected=%s",
                 sent, expected)
        raise
    except Exception as exc:
        log.warning("Native Stars streaming failed: type=%s delivered=%s expected=%s",
                    type(exc).__name__, sent, expected)
        raise
