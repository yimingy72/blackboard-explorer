"""Small async façade over the thread-safe, synchronous MinIO client."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from io import BytesIO
from typing import BinaryIO

import anyio
from minio import Minio
from minio.error import S3Error


class ObjectStore:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ) -> None:
        self.client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self.bucket = bucket

    async def ensure_bucket(self) -> None:
        def ensure() -> None:
            if not self.client.bucket_exists(bucket_name=self.bucket):
                try:
                    self.client.make_bucket(bucket_name=self.bucket)
                except S3Error as exc:
                    if exc.code != "BucketAlreadyOwnedByYou":
                        raise

        await anyio.to_thread.run_sync(ensure)

    async def exists(self, uri: str) -> bool:
        def check() -> bool:
            try:
                self.client.stat_object(bucket_name=self.bucket, object_name=uri)
            except S3Error as exc:
                if exc.code == "NoSuchKey":
                    return False
                raise
            return True

        return await anyio.to_thread.run_sync(check)

    async def put(
        self,
        uri: str,
        data: bytes | BinaryIO,
        length: int = -1,
        content_type: str = "application/octet-stream",
    ) -> None:
        if isinstance(data, bytes):
            length = len(data)
            data = BytesIO(data)
        if length < -1:
            raise ValueError("length must be -1 or non-negative")

        def upload() -> None:
            self.client.put_object(
                bucket_name=self.bucket,
                object_name=uri,
                data=data,
                length=length,
                content_type=content_type,
                part_size=10 * 1024 * 1024 if length == -1 else 0,
            )

        await anyio.to_thread.run_sync(upload)

    async def get(self, uri: str) -> bytes:
        def download() -> bytes:
            response = self.client.get_object(bucket_name=self.bucket, object_name=uri)
            try:
                return response.read()
            finally:
                try:
                    response.close()
                finally:
                    response.release_conn()

        return await anyio.to_thread.run_sync(download)

    async def stream(self, uri: str, chunk_size: int = 65536) -> AsyncGenerator[bytes, None]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        response = await anyio.to_thread.run_sync(
            lambda: self.client.get_object(bucket_name=self.bucket, object_name=uri)
        )
        try:
            while chunk := await anyio.to_thread.run_sync(response.read, chunk_size):
                yield chunk
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(response.close)
                finally:
                    await anyio.to_thread.run_sync(response.release_conn)

    async def list(self, prefix: str = "") -> list[str]:
        return await anyio.to_thread.run_sync(
            lambda: [
                item.object_name
                for item in self.client.list_objects(
                    bucket_name=self.bucket, prefix=prefix, recursive=True
                )
                if item.object_name is not None
            ]
        )

    async def remove(self, uri: str) -> None:
        def delete() -> None:
            try:
                self.client.remove_object(bucket_name=self.bucket, object_name=uri)
            except S3Error as exc:
                if exc.code not in {"NoSuchKey", "NoSuchObject"}:
                    raise

        await anyio.to_thread.run_sync(delete)
