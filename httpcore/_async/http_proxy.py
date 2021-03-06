from ssl import SSLContext
from typing import Tuple

from .._exceptions import ProxyError
from .._types import URL, Headers, Origin, TimeoutDict
from .base import AsyncByteStream
from .connection import AsyncHTTPConnection
from .connection_pool import AsyncConnectionPool, ResponseByteStream


class AsyncHTTPProxy(AsyncConnectionPool):
    """
    A connection pool for making HTTP requests via an HTTP proxy.

    **Parameters:**

    * **proxy_origin** - `Tuple[bytes, bytes, int]` - The address of the proxy
    service as a 3-tuple of (scheme, host, port).
    * **proxy_headers** - `Optional[List[Tuple[bytes, bytes]]]` - A list of
    proxy headers to include.
    * **proxy_mode** - `str` - A proxy mode to operate in. May be "DEFAULT",
    "FORWARD_ONLY", or "TUNNEL_ONLY".
    * **ssl_context** - `Optional[SSLContext]` - An SSL context to use for
    verifying connections.
    * **max_connections** - `Optional[int]` - The maximum number of concurrent
    connections to allow.
    * **max_keepalive** - `Optional[int]` - The maximum number of connections
    to allow before closing keep-alive connections.
    * **http2** - `bool` - Enable HTTP/2 support.
    """

    def __init__(
        self,
        proxy_origin: Origin,
        proxy_headers: Headers = None,
        proxy_mode: str = "DEFAULT",
        ssl_context: SSLContext = None,
        max_connections: int = None,
        max_keepalive: int = None,
        keepalive_expiry: float = None,
        http2: bool = False,
    ):
        assert proxy_mode in ("DEFAULT", "FORWARD_ONLY", "TUNNEL_ONLY")

        self.proxy_origin = proxy_origin
        self.proxy_headers = [] if proxy_headers is None else proxy_headers
        self.proxy_mode = proxy_mode
        super().__init__(
            ssl_context=ssl_context,
            max_connections=max_connections,
            max_keepalive=max_keepalive,
            keepalive_expiry=keepalive_expiry,
            http2=http2,
        )

    async def request(
        self,
        method: bytes,
        url: URL,
        headers: Headers = None,
        stream: AsyncByteStream = None,
        timeout: TimeoutDict = None,
    ) -> Tuple[bytes, int, bytes, Headers, AsyncByteStream]:
        if self._keepalive_expiry is not None:
            await self._keepalive_sweep()

        if (
            self.proxy_mode == "DEFAULT" and url[0] == b"http"
        ) or self.proxy_mode == "FORWARD_ONLY":
            # By default HTTP requests should be forwarded.
            return await self._forward_request(
                method, url, headers=headers, stream=stream, timeout=timeout
            )
        else:
            # By default HTTPS should be tunnelled.
            return await self._tunnel_request(
                method, url, headers=headers, stream=stream, timeout=timeout
            )

    async def _forward_request(
        self,
        method: bytes,
        url: URL,
        headers: Headers = None,
        stream: AsyncByteStream = None,
        timeout: TimeoutDict = None,
    ) -> Tuple[bytes, int, bytes, Headers, AsyncByteStream]:
        """
        Forwarded proxy requests include the entire URL as the HTTP target,
        rather than just the path.
        """
        origin = self.proxy_origin
        connection = await self._get_connection_from_pool(origin)

        if connection is None:
            connection = AsyncHTTPConnection(
                origin=origin, http2=False, ssl_context=self._ssl_context,
            )
            async with self._thread_lock:
                self._connections.setdefault(origin, set())
                self._connections[origin].add(connection)

        # Issue a forwarded proxy request...

        # GET https://www.example.org/path HTTP/1.1
        # [proxy headers]
        # [headers]
        target = b"%b://%b:%d%b" % url
        url = self.proxy_origin + (target,)
        headers = self.proxy_headers + ([] if headers is None else headers)

        response = await connection.request(
            method, url, headers=headers, stream=stream, timeout=timeout
        )
        wrapped_stream = ResponseByteStream(
            response[4], connection=connection, callback=self._response_closed
        )
        return response[0], response[1], response[2], response[3], wrapped_stream

    async def _tunnel_request(
        self,
        method: bytes,
        url: URL,
        headers: Headers = None,
        stream: AsyncByteStream = None,
        timeout: TimeoutDict = None,
    ) -> Tuple[bytes, int, bytes, Headers, AsyncByteStream]:
        """
        Tunnelled proxy requests require an initial CONNECT request to
        establish the connection, and then send regular requests.
        """
        origin = url[:3]
        connection = await self._get_connection_from_pool(origin)

        if connection is None:
            # First, create a connection to the proxy server
            proxy_connection = AsyncHTTPConnection(
                origin=self.proxy_origin, http2=False, ssl_context=self._ssl_context,
            )

            # Issue a CONNECT request...

            # CONNECT www.example.org:80 HTTP/1.1
            # [proxy-headers]
            target = b"%b:%d" % (url[1], url[2])
            connect_url = self.proxy_origin + (target,)
            proxy_response = await proxy_connection.request(
                b"CONNECT", connect_url, headers=self.proxy_headers, timeout=timeout
            )
            proxy_status_code = proxy_response[1]
            proxy_reason_phrase = proxy_response[2]
            proxy_stream = proxy_response[4]

            # Read the response data without closing the socket
            async for _ in proxy_stream:
                pass

            # See if the tunnel was successfully established.
            if proxy_status_code < 200 or proxy_status_code > 299:
                msg = "%d %s" % (proxy_status_code, proxy_reason_phrase.decode("ascii"))
                raise ProxyError(msg)

            # The CONNECT request is successful, so we have now SWITCHED PROTOCOLS.
            # This means the proxy connection is now unusable, and we must create
            # a new one for regular requests, making sure to use the same socket to
            # retain the tunnel.
            connection = AsyncHTTPConnection(
                origin=origin,
                http2=False,
                ssl_context=self._ssl_context,
                socket=proxy_connection.socket,
            )
            await self._add_to_pool(connection)

        # Once the connection has been established we can send requests on
        # it as normal.
        response = await connection.request(
            method, url, headers=headers, stream=stream, timeout=timeout,
        )
        wrapped_stream = ResponseByteStream(
            response[4], connection=connection, callback=self._response_closed
        )
        return response[0], response[1], response[2], response[3], wrapped_stream
