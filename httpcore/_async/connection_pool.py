from ssl import SSLContext
from typing import Callable, Dict, List, Optional, Set, Tuple

from .._threadlock import ThreadLock
from .._url import normalize_url
from .base import AsyncByteStream, AsyncHTTPTransport
from .http11 import AsyncHTTP11Connection, ConnectionState

Origin = Tuple[bytes, bytes, int]
URL = Tuple[bytes, bytes, int, bytes]
Headers = List[Tuple[bytes, bytes]]
TimeoutDict = Dict[str, Optional[float]]


class ResponseByteStream(AsyncByteStream):
    def __init__(
        self,
        stream: AsyncByteStream,
        connection: AsyncHTTP11Connection,
        callback: Callable,
    ) -> None:
        """
        A wrapper around the response stream that we return from `.request()`.

        Ensures that when `stream.close()` is called, the connection pool
        is notified via a callback.
        """
        self.stream = stream
        self.connection = connection
        self.callback = callback

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def close(self):
        try:
            #  Call the underlying stream close callback.
            # This will be a call to `AsyncHTTP11Connection._response_closed()``.
            await self.stream.close()
        finally:
            #  Call the connection pool close callback.
            # This will be a call to `AsyncConnectionPool._response_closed()``.
            await self.callback(self.connection)


class AsyncConnectionPool(AsyncHTTPTransport):
    """
    A connection pool for making HTTP requests.

    **Parameters:**

    * **ssl_context** - `Optional[SSLContext]` - An SSL context to use for verifying connections.
    """

    def __init__(
        self, ssl_context: SSLContext = None,
    ):
        self.ssl_context = SSLContext() if ssl_context is None else ssl_context
        self.connections: Dict[Origin, Set[AsyncHTTP11Connection]] = {}
        self.thread_lock = ThreadLock()

    async def request(
        self,
        method: bytes,
        url: URL,
        headers: Headers = None,
        stream: AsyncByteStream = None,
        timeout: TimeoutDict = None,
    ) -> Tuple[bytes, int, bytes, Headers, AsyncByteStream]:
        url = normalize_url(url)

        origin = url[:3]
        connection = await self._get_connection_from_pool(origin)

        if connection is None:
            connection = AsyncHTTP11Connection(
                origin=origin, ssl_context=self.ssl_context,
            )
            async with self.thread_lock:
                self.connections.setdefault(origin, set())
                self.connections[origin].add(connection)

        response = await connection.request(
            method, url, headers=headers, stream=stream, timeout=timeout
        )
        wrapped_stream = ResponseByteStream(
            response[4], connection=connection, callback=self._response_closed
        )
        return response[0], response[1], response[2], response[3], wrapped_stream

    async def _get_connection_from_pool(
        self, origin: Origin
    ) -> Optional[AsyncHTTP11Connection]:
        # Determine expired keep alive connections on this origin.
        reuse_connection = None
        connections_to_close = set()

        async with self.thread_lock:
            if origin in self.connections:
                connections = self.connections[origin]
                for connection in list(connections):
                    if connection.state == ConnectionState.IDLE:
                        if connection.is_connection_dropped():
                            # IDLE connections that have been dropped should be
                            # removed from the pool.
                            connections_to_close.add(connection)
                            connections.remove(connection)
                        else:
                            # IDLE connections that are still maintained may
                            # be reused.
                            reuse_connection = connection

                # Clean up the connections mapping if we've no connections
                # remaining for this origin.
                if not connections:
                    del self.connections[origin]

            #  Mark the connection as ACTIVE before we return it, so that it
            # will not be re-acquired.
            if reuse_connection is not None:
                reuse_connection.state = ConnectionState.ACTIVE

        # Close any dropped connections.
        for connection in connections_to_close:
            await connection.close()

        return reuse_connection

    async def _response_closed(self, connection: AsyncHTTP11Connection):
        async with self.thread_lock:
            if connection.state == ConnectionState.CLOSED:
                self.connections[connection.origin].remove(connection)
                if not self.connections[connection.origin]:
                    del self.connections[connection.origin]

    async def close(self) -> None:
        connections_to_close = set()

        async with self.thread_lock:
            for connection_set in self.connections.values():
                connections_to_close.update(connection_set)
            self.connections.clear()

        # Close all connections
        for connection in connections_to_close:
            await connection.close()
