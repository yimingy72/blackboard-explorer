"""Construct the shared MinIO object store from runtime settings."""

from urllib.parse import urlsplit

from bbx_objects import ObjectStore

from bbx_runtime.settings import Settings


def object_store(settings: Settings) -> ObjectStore:
    endpoint = urlsplit(settings.minio_endpoint)
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise ValueError("MINIO_ENDPOINT must be an http(s) URL")
    return ObjectStore(
        endpoint.netloc,
        settings.minio_root_user,
        settings.minio_root_password.get_secret_value(),
        settings.minio_bucket,
        secure=endpoint.scheme == "https",
    )
