"""Object storage wrapper checks without a running server."""

import io
import threading

import pytest
from bbx_objects import ObjectStore


class FakeResponse:
    def __init__(self, data):
        self.data = io.BytesIO(data)
        self.closed = False
        self.released = False

    def read(self, size=-1):
        assert threading.current_thread() is not threading.main_thread()
        return self.data.read(size)

    def close(self):
        assert threading.current_thread() is not threading.main_thread()
        self.closed = True

    def release_conn(self):
        assert threading.current_thread() is not threading.main_thread()
        self.released = True


class FakeMinio:
    def __init__(self, *_args, **_kwargs):
        self.objects = {}
        self.responses = []

    def bucket_exists(self, bucket_name):
        assert threading.current_thread() is not threading.main_thread()
        return False

    def make_bucket(self, bucket_name):
        assert threading.current_thread() is not threading.main_thread()

    def put_object(self, bucket_name, object_name, data, length, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        assert kwargs["part_size"] == (10 * 1024 * 1024 if length == -1 else 0)
        self.objects[object_name] = data.read()

    def get_object(self, bucket_name, object_name):
        assert threading.current_thread() is not threading.main_thread()
        response = FakeResponse(self.objects[object_name])
        self.responses.append(response)
        return response

    def stat_object(self, bucket_name, object_name):
        assert threading.current_thread() is not threading.main_thread()
        if object_name not in self.objects:
            raise KeyError(object_name)

    def list_objects(self, bucket_name, prefix, recursive):
        assert threading.current_thread() is not threading.main_thread()
        assert recursive
        return [
            type("Item", (), {"object_name": uri}) for uri in self.objects if uri.startswith(prefix)
        ]

    def remove_object(self, bucket_name, object_name):
        assert threading.current_thread() is not threading.main_thread()
        self.objects.pop(object_name, None)


async def test_put_get_stream_and_cleanup(monkeypatch):
    monkeypatch.setattr("bbx_objects.store.Minio", FakeMinio)
    store = ObjectStore("localhost:9000", "user", "password", "test")
    await store.ensure_bucket()
    await store.put("a/one", b"abcdef")
    await store.put("a/two", io.BytesIO(b"xyz"))
    assert await store.exists("a/one")
    assert await store.get("a/one") == b"abcdef"
    assert [part async for part in store.stream("a/one", chunk_size=2)] == [b"ab", b"cd", b"ef"]
    assert await store.list("a/") == ["a/one", "a/two"]
    await store.remove("a/one")
    await store.remove("a/one")
    assert await store.list("a/") == ["a/two"]
    client = store.client
    assert isinstance(client, FakeMinio)
    assert all(response.closed and response.released for response in client.responses)


async def test_stream_close_after_cancellation(monkeypatch):
    monkeypatch.setattr("bbx_objects.store.Minio", FakeMinio)
    store = ObjectStore("localhost:9000", "user", "password", "test")
    await store.put("one", b"abcdef")
    stream = store.stream("one", chunk_size=2)
    assert await anext(stream) == b"ab"
    await stream.aclose()
    client = store.client
    assert isinstance(client, FakeMinio)
    assert client.responses[0].closed and client.responses[0].released


async def test_invalid_lengths(monkeypatch):
    monkeypatch.setattr("bbx_objects.store.Minio", FakeMinio)
    store = ObjectStore("localhost:9000", "user", "password", "test")
    with pytest.raises(ValueError, match="length"):
        await store.put("one", io.BytesIO(b"a"), length=-2)
    with pytest.raises(ValueError, match="chunk_size"):
        await anext(store.stream("one", chunk_size=0))
