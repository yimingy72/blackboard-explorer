"""HTTPS destination binding for the existing external MCP SDK client."""

import httpx


class _BoundedResponse(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self):
        remaining = 2 * 1024 * 1024
        async for chunk in self.stream:
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("MCP response exceeds the byte bound")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class PinnedMcpTransport(httpx.AsyncBaseTransport):
    def __init__(self, url: str, address: str):
        self.url = httpx.URL(url)
        self.address = address
        self.transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url != self.url or request.method not in {"POST", "GET", "DELETE"}:
            raise httpx.RequestError("The frozen MCP destination changed", request=request)
        headers = httpx.Headers(request.headers)
        headers["Host"] = self.url.netloc.decode("ascii")
        headers["Accept-Encoding"] = "identity"
        # Keep the original TLS identity while connecting to the approved resolved address.
        pinned = httpx.Request(
            request.method,
            self.url.copy_with(host=self.address),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.url.host},
        )
        response = await self.transport.handle_async_request(pinned)
        if (
            response.is_redirect
            or response.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            await response.aclose()
            raise ValueError("MCP server used a redirect or unsupported response encoding")
        assert isinstance(response.stream, httpx.AsyncByteStream)
        response.stream = _BoundedResponse(response.stream)
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()
