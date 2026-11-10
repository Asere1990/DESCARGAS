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
        self.endpoint = os.getenv("GALERIAS_R2_ENDPOINT", "").strip()
        self.key = os.getenv("GALERIAS_R2_ACCESS_KEY_ID", "").strip()
        self.secret = os.getenv("GALERIAS_R2_SECRET_ACCESS_KEY", "").strip()
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

    async def exists(self,key):
        key=self.safe_key(key)
        def check():
            try:
                self._client().head_object(Bucket=self.bucket,Key=key)
                return True
            except self._client().exceptions.NoSuchKey:
                return False
        return await asyncio.to_thread(check)
