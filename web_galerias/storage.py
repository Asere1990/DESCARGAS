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

    async def upload_stream(self, chunks, key, mime, check=None):
        """Video a R2 en multipart de 8 MiB, sin usar disco ni cargarlo en RAM."""
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
        try:
            async for chunk in chunks:
                if check is not None:
                    await check()
                if not chunk:
                    continue
                buffer.extend(chunk)
                total += len(chunk)
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
            if buffer:
                if check is not None:
                    await check()
                result = await asyncio.to_thread(
                    client.upload_part, Bucket=self.bucket, Key=key,
                    UploadId=upload_id, PartNumber=number, Body=bytes(buffer))
                parts.append({"ETag": result["ETag"], "PartNumber": number})
            if not parts:
                raise ValueError("El video llegó vacío desde Telegram.")
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
            # Nunca dejar deliberadamente una subida multipart incompleta facturando.
            try:
                await asyncio.shield(asyncio.to_thread(
                    client.abort_multipart_upload, Bucket=self.bucket, Key=key,
                    UploadId=upload_id))
            except Exception:
                pass
            raise
