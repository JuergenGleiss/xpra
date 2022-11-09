# This file is part of Xpra.
# Copyright (C) 2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from typing import Dict, List, Optional, Union

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.logger import QuicLogger
from aioquic.h0.connection import H0_ALPN, H0Connection
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import (
    DatagramReceived,
    DataReceived,
    H3Event,
    HeadersReceived,
    WebTransportStreamDataReceived,
)
from aioquic.quic.events import DatagramFrameReceived, ProtocolNegotiated, QuicEvent
from xpra.net.quic.common import MAX_DATAGRAM_FRAME_SIZE
from xpra.net.quic.http_request_handler import HttpRequestHandler
from xpra.net.quic.websocket_request_handler import WebSocketHandler
#from xpra.net.quic.webtransport_request_handler import WebTransportHandler
from xpra.net.quic.session_ticket_store import SessionTicketStore
from xpra.net.quic.asyncio_thread import get_threaded_loop
from xpra.util import ellipsizer
from xpra.log import Logger
log = Logger("quic")

quic_logger = QuicLogger()

HttpConnection = Union[H0Connection, H3Connection]
Handler = Union[HttpRequestHandler, WebSocketHandler]


class HttpServerProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs) -> None:
        self._xpra_server = kwargs.pop("xpra_server", None)
        log(f"HttpServerProtocol({args}, {kwargs}) xpra-server={self._xpra_server}")
        super().__init__(*args, **kwargs)
        self._handlers: Dict[int, Handler] = {}
        self._http: Optional[HttpConnection] = None

    def quic_event_received(self, event: QuicEvent) -> None:
        log(f"hsp:quic_event_received(%s)", ellipsizer(event))
        if isinstance(event, ProtocolNegotiated):
            if event.alpn_protocol in H3_ALPN:
                self._http = H3Connection(self._quic, enable_webtransport=True)
            elif event.alpn_protocol in H0_ALPN:
                self._http = H0Connection(self._quic)
        elif isinstance(event, DatagramFrameReceived):
            if event.data == b"quack":
                self._quic.send_datagram_frame(b"quack-ack")
        #  pass event to the HTTP layer
        log(f"hsp:quic_event_received(..) http={self._http}")
        if self._http is not None:
            for http_event in self._http.handle_event(event):
                self.http_event_received(http_event)

    def http_event_received(self, event: H3Event) -> None:
        handler = self._handlers.get(event.stream_id)
        log(f"hsp:http_event_received(%s) handler for stream id {event.stream_id}: {handler}", ellipsizer(event))
        if isinstance(event, HeadersReceived) and not handler:
            handler = self.new_http_handler(event)
            handler.xpra_server = self._xpra_server
            self._handlers[event.stream_id] = handler
            #asyncio.ensure_future(handler.run_asgi(self.app))
            #return
        if isinstance(event, (DataReceived, HeadersReceived)) and handler:
            handler.http_event_received(event)
            return
        if isinstance(event, DatagramReceived):
            handler = self._handlers[event.flow_id]
            handler.http_event_received(event)
            return
        if isinstance(event, WebTransportStreamDataReceived):
            handler = self._handlers[event.session_id]
            handler.http_event_received(event)

    def new_http_handler(self, event) -> Handler:
        authority = None
        headers = []
        raw_path = b""
        method = ""
        protocol = None
        for header, value in event.headers:
            if header == b":authority":
                authority = value
                headers.append((b"host", value))
            elif header == b":method":
                method = value.decode()
            elif header == b":path":
                raw_path = value
            elif header == b":protocol":
                protocol = value.decode()
            elif header and not header.startswith(b":"):
                headers.append((header, value))
        if b"?" in raw_path:
            path_bytes, query_string = raw_path.split(b"?", maxsplit=1)
        else:
            path_bytes, query_string = raw_path, b""
        path = path_bytes.decode()
        log.info("HTTP request %s %s", method, path)

        # FIXME: add a public API to retrieve peer address
        client_addr = self._http._quic._network_paths[0].addr

        scope = {
            "client": (client_addr[0], client_addr[1]),
            "headers": headers,
            "http_version": "0.9" if isinstance(self._http, H0Connection) else "3",
            "method": method,
            "path": path,
            "query_string": query_string,
            "raw_path": raw_path,
        }
        if method == "CONNECT" and protocol == "websocket":
            subprotocols: List[str] = []
            for header, value in event.headers:
                if header == b"sec-websocket-protocol":
                    subprotocols = [x.strip() for x in value.decode().split(",")]
            scope.update({
                "subprotocols"  : subprotocols,
                "type"          : "websocket",
                "scheme"        : "wss",
                })
            return WebSocketHandler(connection=self._http, scope=scope,
                                    stream_id=event.stream_id,
                                    transmit=self.transmit)

        if method == "CONNECT" and protocol == "webtransport":
            scope.update({
                "scheme"        : "https",
                "type"          : "webtransport",
            })
            raise RuntimeError("no WebTransport support yet")
            #return WebTransportHandler(connection=self._http, scope=scope,
            #                           stream_id=event.stream_id,
            #                           transmit=self.transmit)

        #extensions: Dict[str, Dict] = {}
        #if isinstance(self._http, H3Connection):
        #    extensions["http.response.push"] = {}
        scope.update({
            "scheme": "https",
            "type": "http",
        })
        return HttpRequestHandler(xpra_server=self._xpra_server,
                                  authority=authority, connection=self._http,
                                  protocol=self,
                                  scope=scope,
                                  stream_id=event.stream_id,
                                  transmit=self.transmit)


async def do_listen(quic_sock, xpra_server):
    log(f"do_listen({quic_sock}, {xpra_server})")
    def create_protocol(*args, **kwargs):
        log("create_protocol!")
        return HttpServerProtocol(*args, xpra_server=xpra_server, **kwargs)
    try:
        configuration = QuicConfiguration(
            alpn_protocols=H3_ALPN + H0_ALPN + ["siduck"],
            is_client=False,
            max_datagram_frame_size=MAX_DATAGRAM_FRAME_SIZE,
            quic_logger=quic_logger,
        )
        configuration.load_cert_chain(quic_sock.ssl_cert, quic_sock.ssl_key)
        log(f"quic configuration={configuration}")
        session_ticket_store = SessionTicketStore()
        await serve(
            quic_sock.host,
            quic_sock.port,
            configuration=configuration,
            create_protocol=create_protocol,
            session_ticket_fetcher=session_ticket_store.pop,
            session_ticket_handler=session_ticket_store.add,
            retry=quic_sock.retry,
        )
    except Exception:
        log.error(f"Error: listening on {quic_sock}", exc_info=True)
        raise

def listen_quic(quic_sock, xpra_server=None):
    log(f"listen_quic({quic_sock})")
    t = get_threaded_loop()
    t.call(do_listen(quic_sock, xpra_server))
    return quic_sock.close