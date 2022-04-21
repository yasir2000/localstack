import asyncio
import io
import logging
import math
import typing as t
from asyncio import AbstractEventLoop
from concurrent.futures import Executor
from urllib.parse import urlparse

if t.TYPE_CHECKING:
    from _typeshed import WSGIApplication, WSGIEnvironment
    from asgiref.typing import ASGIReceiveCallable, ASGISendCallable, HTTPScope, Scope

LOG = logging.getLogger(__name__)


def populate_wsgi_environment(environ: "WSGIEnvironment", scope: "HTTPScope"):
    """
    Adds the non-IO parts (e.g., excluding wsgi.input) from the ASGI HTTPScope to the WSGI Environment.

    :param environ: the WSGI environment to populate
    :param scope: the ASGI scope as source
    """
    environ["REQUEST_METHOD"] = scope["method"]
    # path/uri info
    environ["SCRIPT_NAME"] = scope.get("root_path", "").rstrip("/")
    path = scope["path"]
    path = path if path[0] == "/" else urlparse(path).path
    environ["PATH_INFO"] = path
    environ["RAW_URI"] = scope["raw_path"]

    query_string = scope.get("query_string")
    if query_string:
        environ["QUERY_STRING"] = query_string.decode("latin1")

    # server address / host
    server = scope.get("server") or ("localhost", 80)
    environ["SERVER_NAME"] = server[0]
    environ["SERVER_PORT"] = str(server[1]) if server[1] else "80"

    # http version
    environ["SERVER_PROTOCOL"] = "HTTP/" + scope["http_version"]

    # client (remote) address
    client = scope.get("client")
    if client:
        environ["REMOTE_ADDR"] = client[0]
        environ["REMOTE_PORT"] = str(client[1])

    # headers
    for name, value in scope["headers"]:
        key = name.decode("latin1").upper().replace("-", "_")

        if key not in ["CONTENT_TYPE", "CONTENT_LENGTH"]:
            key = f"HTTP_{key}"

        environ[key] = value.decode("latin1")

    # wsgi specific keys
    environ["wsgi.version"] = (1, 0)
    environ["wsgi.url_scheme"] = scope.get("scheme", "http")
    environ["wsgi.errors"] = io.BytesIO()
    environ["wsgi.multithread"] = True
    environ["wsgi.multiprocess"] = False
    environ["wsgi.run_once"] = False


class HTTPRequestEventStreamAdapter:
    """
    Minimal adapter to expose an ASGIReceiveCallable coroutine that returns HTTPRequestEvent
    instances, as an IO[bytes] stream for consumption in synchronous WSGI/Werkzeug code.
    """

    def __init__(
        self, receive: "ASGIReceiveCallable", event_loop: t.Optional[AbstractEventLoop] = None
    ) -> None:
        super().__init__()
        self.receive = receive
        self.event_loop = event_loop or asyncio.get_event_loop()

    def read(self, size: int = -1) -> bytes:
        # TODO: better implementation
        # TODO: disconnect events

        if size != -1:
            raise NotImplementedError("can only read from stream exhaustively")

        arr = bytearray()

        more = True
        while more:
            recv_future = asyncio.run_coroutine_threadsafe(self.receive(), self.event_loop)
            event = recv_future.result()
            body = event["body"]
            if body:
                arr.extend(body)
            more = event.get("more_body", False)

        return arr


class WsgiStartResponse:
    def __init__(
        self,
        send: "ASGISendCallable",
        event_loop: AbstractEventLoop = None,
    ):
        self.send = send
        self.event_loop = event_loop or asyncio.get_event_loop()
        self.sent = 0
        self.content_length = math.inf
        self.finalized = False
        self.started = False

    def __call__(
        self, status: str, headers: t.List[t.Tuple[str, str]], exec_info=None
    ) -> t.Callable[[bytes], t.Any]:
        return self.start_response_sync(status, headers, exec_info)

    def start_response_sync(
        self, status: str, headers: t.List[t.Tuple[str, str]], exec_info=None
    ) -> t.Callable[[bytes], t.Any]:
        send = self.send
        loop = self.event_loop

        # start sending response
        asyncio.run_coroutine_threadsafe(
            send(
                {
                    "type": "http.response.start",
                    "status": int(status[:3]),
                    "headers": [(h[0].encode("latin1"), h[1].encode("latin1")) for h in headers],
                }
            ),
            loop,
        ).result()

        self.started = True
        # find out content length if set
        self.content_length = math.inf  # unknown content-length
        for k, v in headers:
            if k.lower() == "content-length":
                self.content_length = int(v)
                break

        return self.write_sync

    def write_sync(self, data: bytes) -> None:
        return asyncio.run_coroutine_threadsafe(self.write(data), self.event_loop).result()

    async def write(self, data: bytes) -> None:
        if not self.started:
            raise ValueError("not started the response yet")
        await self.send({"type": "http.response.body", "body": data, "more_body": True})
        self.sent += len(data)
        if self.sent >= self.content_length:
            await self.close()

    async def close(self):
        if not self.started:
            raise ValueError("not started the response yet")

        if not self.finalized:
            self.finalized = True
            await self.send({"type": "http.response.body", "body": b"", "more_body": False})


async def to_async_generator(
    it: t.Iterator,
    loop: t.Optional[AbstractEventLoop] = None,
    executor: t.Optional[Executor] = None,
) -> t.AsyncGenerator:
    """
    Wraps a given synchronous Iterator as an async generator, where each invocation to ``next(it)``
    will be wrapped in a coroutine execution.

    :param it: the iterator to wrap
    :param loop: the event loop to run the next invocations
    :param executor: the executor to run the synchronous code
    :return: an async generator
    """
    loop = loop or asyncio.get_event_loop()
    stop = object()

    def _next_sync():
        try:
            # this call may potentially call blocking IO, which is why we call it in an executor
            return next(it)
        except StopIteration:
            return stop

    while True:
        val = await loop.run_in_executor(executor, _next_sync)
        if val is stop:
            return
        yield val


class ASGIAdapter:
    """
    Adapter to expose a WSGIApplication as an ASGI3Application. This allows you to serve synchronous WSGI applications
    through ASGI servers (e.g., Hypercorn).

    https://asgi.readthedocs.io/en/latest/specs/main.html
    """

    def __init__(
        self,
        wsgi_app: "WSGIApplication",
        event_loop: AbstractEventLoop = None,
        executor: Executor = None,
    ):
        self.wsgi_app = wsgi_app
        self.event_loop = event_loop or asyncio.get_event_loop()
        self.executor = executor

    async def __call__(
        self, scope: "Scope", receive: "ASGIReceiveCallable", send: "ASGISendCallable"
    ):
        """
        The ASGI 3 interface. Can only handle HTTP calls.

        :param scope: the connection scope
        :param receive: the receive callable
        :param send: the send callable
        """
        if scope["type"] == "http":
            return await self.handle_http(scope, receive, send)

        raise NotImplementedError("Unhandled protocol %s" % scope["type"])

    def to_wsgi_environment(
        self,
        scope: "HTTPScope",
        receive: "ASGIReceiveCallable",
    ) -> "WSGIEnvironment":
        """
        Creates an IO-ready WSGIEnvironment from the given ASGI HTTP call.

        :param scope: the ASGI HTTP Scope
        :param receive: the ASGI callable to receive the HTTP request
        :return: a WSGIEnvironment
        """
        environ: "WSGIEnvironment" = {}
        populate_wsgi_environment(environ, scope)
        # add IO wrappers
        environ["wsgi.input"] = HTTPRequestEventStreamAdapter(receive, event_loop=self.event_loop)
        environ["wsgi.input_terminated"] = True
        return environ

    async def handle_http(
        self, scope: "HTTPScope", receive: "ASGIReceiveCallable", send: "ASGISendCallable"
    ):
        env = self.to_wsgi_environment(scope, receive)

        response = WsgiStartResponse(send, self.event_loop)

        iterable = await self.event_loop.run_in_executor(
            self.executor, self.wsgi_app, env, response
        )

        try:
            if iterable:
                # Generators are also Iterators
                if isinstance(iterable, t.Iterator):
                    iterable = to_async_generator(iterable)

                if isinstance(iterable, (t.AsyncIterator, t.AsyncIterable)):
                    async for packet in iterable:
                        await response.write(packet)
                else:
                    for packet in iterable:
                        await response.write(packet)
        finally:
            await response.close()
