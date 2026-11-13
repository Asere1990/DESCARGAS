"""Almacenamiento privado, independiente de Telegram, compatible con Cloudflare R2."""
from __future__ import annotations
import asyncio
import os
import re
from pathlib import Path

class NotConfigured(RuntimeError):
    pass

class PrivateStorage:
    def __init__(self):
        self.bucket = os.getenv("GALERIAS_R2_BUCKET", "").strip()
        self.endpoint = (os.getenv("CLOUD_R2") or os.getenv("GALERIAS_R2_ENDPOINT") or "").strip()
        self.key = (os.getenv("CLOUD_ID") or os.getenv("GALERIAS_R2_ACCESS_KEY_ID") or "").strip()
        self.secret = (os.getenv("CLOUD_SECRET") or os.getenv("GALERIAS_R2_SECRET_ACCESS_KEY") or "").strip()
        self.client = None

    @property
    def ready(self):
        return bool(self.bucket and self.endpoint.startswith("https://") and self.key and self.secret)

    def _client(self):
        if not self.ready:
            raise NotConfigured("Configura las credenciales del almacenamiento privado antes de respaldar.")
        if self.client is None:
            import boto3
            from botocore.config import Config
            self.client = boto3.client(
                "s3", endpoint_url=self.endpoint, region_name="auto",
                aws_access_key_id=self.key, aws_secret_access_key=self.secret,
                config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=90,
                              retries={"max_attempts":2, "mode":"standard"}))
        return self.client

    @staticmethod
    def safe_key(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_./-]{1,240}", value) or ".." in value:
            raise ValueError("Clave de almacenamiento incorrecta.")
        return value

    async def upload(self, path, key, mime):
        key = self.safe_key(key)
        def send():
            with open(path, "rb") as stream:
                self._client().upload_fileobj(stream, self.bucket, key,
                    ExtraArgs={"ContentType": mime, "CacheControl":"private, max-age=3600"})
        await asyncio.to_thread(send)

    def signed_get(self, key, filename=None, duration=180):
        key = self.safe_key(key)
        args={"Bucket":self.bucket,"Key":key}
        if filename:
            sanitized=re.sub(r"[^a-zA-Z0-9._-]", "_", filename)[:100] or "archivo"
            args["ResponseContentDisposition"]=f'attachment; filename="{sanitized}"'
        return self._client().generate_presigned_url(
            "get_object", Params=args, ExpiresIn=min(300,max(30,int(duration))))

    async def object_size(self, key):
        """Tamaño de un objeto ya publicado, para reanudar sin subirlo dos veces."""
        from botocore.exceptions import ClientError
        key = self.safe_key(key)
        def head():
            try:
                data = self._client().head_object(Bucket=self.bucket, Key=key)
                return int(data["ContentLength"])
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code", "")) in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise
        return await asyncio.to_thread(head)

    async def exists(self, key):
        return await self.object_size(key) is not None

    async def upload_stream(self, chunks, key, mime, check=None, progress=None, expected_size=None):
        """Vídeo Telegram -> R2 con lectura anticipada acotada y verificación completa.

        Una única descarga Telegram. Mientras R2 sube una parte, una cola
        pequeña puede seguir leyendo Telegram; nunca guarda el vídeo completo.
        """
        key = self.safe_key(key)
        client = self._client()
        create = await asyncio.to_thread(
            client.create_multipart_upload, Bucket=self.bucket, Key=key,
            ContentType=mime, CacheControl="private, max-age=3600")
        upload_id = create["UploadId"]
        parts = []
        number = 1
        buffer = bytearray()
        total = 0
        # 8 trozos Telegram de hasta 512 KiB: presión de retorno automática.
        queue = asyncio.Queue(maxsize=8)

        async def prefetch():
            try:
                async for chunk in chunks:
                    if check is not None:
                        await check()
                    if chunk:
                        await queue.put(bytes(chunk))
            finally:
                await queue.put(None)

        producer = asyncio.create_task(prefetch())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    await producer  # Propaga errores reales del origen Telegram.
                    break
                buffer.extend(chunk)
                total += len(chunk)
                # Actual Telegram bytes consumed by the R2 upload pipeline.
                # A caller can publish progress at a throttled cadence;
                # no full video is ever kept in RAM.
                if progress is not None:
                    await progress(total)
                while len(buffer) >= 8 * 1024 * 1024:
                    body = bytes(buffer[:8 * 1024 * 1024])
                    del buffer[:8 * 1024 * 1024]
                    if check is not None:
                        await check()
                    result = await asyncio.to_thread(
                        client.upload_part, Bucket=self.bucket, Key=key,
                        UploadId=upload_id, PartNumber=number, Body=body)
                    parts.append({"ETag": result["ETag"], "PartNumber": number})
                    number += 1
            # A real MP4 needs its movie metadata. A 1024-byte fragment
            # containing only ftyp/mdat must never be published as playable.
            if mime=="video/mp4" and 0<total<=4096 and b"moov" not in buffer:
                raise ValueError("MP4 incompleto: faltan metadatos.")
            if expected_size is not None and int(expected_size)>0 and total!=int(expected_size):
                raise ValueError("Video Telegram incompleto: tamaño no coincide.")
            if buffer:
                if check is not None:
                    await check()
                result = await asyncio.to_thread(
                    client.upload_part, Bucket=self.bucket, Key=key,
                    UploadId=upload_id, PartNumber=number, Body=bytes(buffer))
                parts.append({"ETag": result["ETag"], "PartNumber": number})
            if not parts:
                raise ValueError("El video llegó vacío desde Telegram.")
            # A media object must NEVER be published from a short Telegram
            # response. An earlier 1024-byte MP4 was incorrectly marked ready.
            if expected_size is not None and int(expected_size)>0 and total!=int(expected_size):
                raise ValueError("Video Telegram incompleto: tamaño no coincide.")
            if check is not None:
                await check()
            await asyncio.to_thread(
                client.complete_multipart_upload, Bucket=self.bucket, Key=key,
                UploadId=upload_id, MultipartUpload={"Parts": parts})
            remote = await self.object_size(key)
            if remote != total:
                raise ValueError("La copia guardada no coincide con el tamaño descargado.")
            return total
        except BaseException:
            # Cancelar la lectura Telegram, esperar su cierre y abortar R2
            # sin publicar objetos incompletos ni dejar multipartes abiertos.
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            try:
                await asyncio.shield(asyncio.to_thread(
                    client.abort_multipart_upload, Bucket=self.bucket, Key=key,
                    UploadId=upload_id))
            except Exception:
                pass
            raise
