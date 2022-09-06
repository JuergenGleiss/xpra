# This file is part of Xpra.
# Copyright (C) 2017-2021 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import struct
from socket import error as socket_error
from queue import Queue

from xpra.os_util import hexstr
from xpra.util import repr_ellipsized, envint
from xpra.make_thread import make_thread, start_thread
from xpra.net.protocol import force_flush_queue, exit_queue, INVALID, CONNECTION_LOST
from xpra.net.common import ConnectionClosedException          #@UndefinedVariable (pydev false positive)
from xpra.net.bytestreams import ABORT
from xpra.net.rfb.rfb_const import RFBClientMessage, CLIENT_PACKET_TYPE_STR, PACKET_STRUCT
from xpra.log import Logger

log = Logger("network", "protocol", "rfb")

RFB_LOG = os.environ.get("XPRA_RFB_LOG")
READ_BUFFER_SIZE = envint("XPRA_READ_BUFFER_SIZE", 65536)

PROTOCOL_VERSION = (3, 8)


class RFBProtocol:

    TYPE = "rfb"

    def __init__(self, scheduler, conn, process_packet_cb, data=b""):
        assert scheduler is not None
        assert conn is not None
        self.timeout_add = scheduler.timeout_add
        self.idle_add = scheduler.idle_add
        self._conn = conn
        self._process_packet_cb = process_packet_cb
        self._write_queue = Queue()
        self._buffer = data
        self._challenge = None
        self.share = False
        #counters:
        self.input_packetcount = 0
        self.input_raw_packetcount = 0
        self.output_packetcount = 0
        self.output_raw_packetcount = 0
        self._closed = False
        self._packet_parser = self._parse_protocol_handshake
        self._write_thread = None
        self._read_thread = make_thread(self._read_thread_loop, "read", daemon=True)
        self.log = None
        if RFB_LOG:
            self.log = open(RFB_LOG, "w")


    def is_closed(self):
        return self._closed

    def is_sending_encrypted(self):
        return False


    def send_protocol_handshake(self):
        self.send(b"RFB 003.008\n")

    def _parse_invalid(self, packet):
        return len(packet)

    def _parse_protocol_handshake(self, packet):
        log("parse_protocol_handshake(%r)", packet)
        if len(packet)<12:
            return 0
        if not packet.startswith(b'RFB '):
            self.invalid_header(self, packet, "invalid RFB protocol handshake packet header")
            return 0
        #ie: packet==b'RFB 003.008\n'
        protocol_version = tuple(int(x) for x in packet[4:11].split(b"."))
        if protocol_version!=PROTOCOL_VERSION:
            msg = "unsupported protocol version"
            log.error("Error: %s", msg)
            self.send(struct.pack(b"!BI", 0, len(msg))+msg)
            self.invalid(msg, packet)
            return 0
        self.handshake_complete()
        return 12

    def handshake_complete(self):
        raise NotImplementedError

    def _parse_security_handshake(self, packet):
        raise NotImplementedError

    def _parse_challenge(self, response):
        raise NotImplementedError

    def _parse_security_result(self, packet):
        raise NotImplementedError

    def _parse_rfb(self, packet):
        try:
            ptype = ord(packet[0])
        except TypeError:
            ptype = packet[0]
        packet_type = CLIENT_PACKET_TYPE_STR.get(ptype)
        if not packet_type:
            self.invalid("unknown RFB packet type: %#x" % ptype, packet)
            return 0
        s = PACKET_STRUCT.get(ptype)     #ie: Struct("!BBBB")
        if not s:
            self.invalid("RFB packet type '%s' is not supported" % packet_type, packet)
            return 0
        if len(packet)<s.size:
            return 0
        size = s.size
        values = list(s.unpack(packet[:size]))
        values[0] = packet_type
        #some packets require parsing extra data:
        if ptype==RFBClientMessage.SetEncodings:
            N = values[2]
            estruct = struct.Struct(b"!"+b"i"*N)
            size += estruct.size
            if len(packet)<size:
                return 0
            encodings = estruct.unpack(packet[s.size:size])
            values.append(encodings)
        elif ptype==RFBClientMessage.ClientCutText:
            l = values[4]
            size += l
            if len(packet)<size:
                return 0
            text = packet[s.size:size]
            values.append(text)
        self.input_packetcount += 1
        log("RFB packet: %s: %s", packet_type, values[1:])
        #now trigger the callback:
        self._process_packet_cb(self, values)
        #return part of packet not consumed:
        return size


    def __repr__(self):
        return "RFBProtocol(%s)" % self._conn

    def get_threads(self):
        return tuple(x for x in (
            self._write_thread,
            self._read_thread,
            ) if x is not None)


    def get_info(self, *_args):
        info = {"protocol" : PROTOCOL_VERSION}
        for t in self.get_threads():
            info.setdefault("thread", {})[t.name] = t.is_alive()
        return info


    def start(self):
        def start_network_read_thread():
            if not self._closed:
                self._read_thread.start()
        self.idle_add(start_network_read_thread)


    def send_disconnect(self, *_args, **_kwargs):
        #no such packet in RFB, just close
        self.close()


    def queue_size(self):
        return self._write_queue.qsize()

    def send(self, packet):
        if self._closed:
            log("connection is closed already, not sending packet")
            return
        if log.is_debug_enabled():
            if len(packet)<=16:
                log("send(%i bytes: %s)", len(packet), hexstr(packet))
            else:
                from xpra.simple_stats import std_unit  #pylint: disable=import-outside-toplevel
                log("send(%s bytes: %s..)", std_unit(len(packet)), hexstr(packet[:16]))
        if self.log:
            self.log.write(f"send: {hexstr(packet)}\n")
        if self._write_thread is None:
            self.start_write_thread()
        self._write_queue.put(packet)

    def start_write_thread(self):
        log("rfb: starting write thread")
        self._write_thread = start_thread(self._write_thread_loop, "write", daemon=True)

    def _io_thread_loop(self, name, callback):
        try:
            log("io_thread_loop(%s, %s) loop starting", name, callback)
            while not self._closed and callback():
                pass
            log("io_thread_loop(%s, %s) loop ended, closed=%s", name, callback, self._closed)
        except ConnectionClosedException as e:
            log("%s closed", self._conn, exc_info=True)
            if not self._closed:
                #ConnectionClosedException means the warning has been logged already
                self._connection_lost("%s connection %s closed" % (name, self._conn))
        except (OSError, socket_error) as e:
            if not self._closed:
                self._internal_error("%s connection %s reset" % (name, self._conn), e, exc_info=e.args[0] not in ABORT)
        except Exception as e:
            #can happen during close(), in which case we just ignore:
            if not self._closed:
                log.error("Error: %s on %s failed: %s", name, self._conn, type(e), exc_info=True)
                self.close()

    def _write_thread_loop(self):
        self._io_thread_loop("write", self._write)
    def _write(self):
        buf = self._write_queue.get()
        # Used to signal that we should exit:
        if buf is None:
            log("write thread: empty marker, exiting")
            self.close()
            return False
        con = self._conn
        if not con:
            return False
        while buf and not self._closed:
            written = con.write(buf)
            if written:
                buf = buf[written:]
                self.output_raw_packetcount += 1
        self.output_packetcount += 1
        return True

    def _read_thread_loop(self):
        self._io_thread_loop("read", self._read)
    def _read(self):
        c = self._conn
        if not c:
            return None
        buf = c.read(READ_BUFFER_SIZE)
        #log("read()=%i bytes (%s)", len(buf or b""), type(buf))
        if not buf:
            log("read thread: eof")
            #give time to the parse thread to call close itself
            #so it has time to parse and process the last packet received
            self.timeout_add(1000, self.close)
            return False
        if self.log:
            self.log.write(f"receive: {hexstr(buf)}\n")
        self.input_raw_packetcount += 1
        self._buffer += buf
        #log("calling %s(%s)", self._packet_parser, repr_ellipsized(self._buffer))
        while self._buffer:
            consumed = self._packet_parser(self._buffer)
            if consumed==0:
                break
            self._buffer = self._buffer[consumed:]
        return True

    def _internal_error(self, message="", exc=None, exc_info=False):
        #log exception info with last log message
        if self._closed:
            return
        ei = exc_info
        if exc:
            ei = None   #log it separately below
        log.error("Error: %s", message, exc_info=ei)
        if exc:
            log.error(" %s", exc, exc_info=exc_info)
        self.idle_add(self._connection_lost, message)

    def _connection_lost(self, message="", exc_info=False):
        log("connection lost: %s", message, exc_info=exc_info)
        self.close()
        return False


    def invalid(self, msg, data):
        log("invalid(%s, %r)", msg, data)
        self._packet_parser = self._parse_invalid
        self.idle_add(self._process_packet_cb, self, [INVALID, msg, data])
        # Then hang up:
        self.timeout_add(1000, self._connection_lost, msg)


    #delegates to invalid_header()
    def invalid_header(self, proto, data, msg=""):
        log("invalid_header%s", (proto, data, msg))
        self._invalid_header(proto, data, msg)

    def _invalid_header(self, _proto, data, msg="invalid packet header"):
        self._packet_parser = self._parse_invalid
        err = "%s: '%s'" % (msg, hexstr(data[:8]))
        if len(data)>1:
            err += " read buffer=%s (%i bytes)" % (repr_ellipsized(data), len(data))
        self.invalid(err, data)


    def gibberish(self, msg, data):
        log("gibberish(%s, %r)", msg, data)
        self.close()


    def close(self):
        log("RFBProtocol.close() closed=%s, connection=%s", self._closed, self._conn)
        if self._closed:
            return
        self._closed = True
        #self.idle_add(self._process_packet_cb, self, [Protocol.CONNECTION_LOST])
        c = self._conn
        if c:
            try:
                log("RFBProtocol.close() calling %s", c.close)
                c.close()
            except IOError:
                log.error("Error closing %s", self._conn, exc_info=True)
            self._conn = None
        self.terminate_queue_threads()
        #log.error("sending connection-lost")
        self._process_packet_cb(self, [CONNECTION_LOST])
        self.idle_add(self.clean)
        if self.log:
            self.log.close()
            self.log = None
        log("RFBProtocol.close() done")

    def clean(self):
        #clear all references to ensure we can get garbage collected quickly:
        self._write_thread = None
        self._read_thread = None
        self._process_packet_cb = None

    def terminate_queue_threads(self):
        log("terminate_queue_threads()")
        #make all the queue based threads exit by adding the empty marker:
        owq = self._write_queue
        self._write_queue = exit_queue()
        force_flush_queue(owq)
