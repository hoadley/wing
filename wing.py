#!/usr/bin/env python3
"""Discover and connect to a Behringer WING console over OSC (asyncio).

Discovery works by broadcasting the string "WING?" to UDP port 2222; every
console on the subnet answers with a comma-separated identity string:

    WING,<ip>,<name>,<model>,<serial>,<firmware>

OSC control/query traffic then goes to UDP port 2223 on the console.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from pythonosc.osc_message import OscMessage, ParseError
from pythonosc.osc_message_builder import OscMessageBuilder

DISCOVERY_PORT = 2222
OSC_PORT = 2223
DISCOVERY_REQUEST = b"WING?"
DISCOVERY_PREFIX = "WING,"


@dataclass(frozen=True)
class WingInfo:
    """Identity of a discovered WING console."""

    ip: str
    name: str
    model: str
    serial: str
    firmware: str

    @classmethod
    def from_reply(cls, reply: str) -> "WingInfo":
        fields = reply.split(",")
        if len(fields) < 6 or fields[0] != "WING":
            raise ValueError(f"not a WING discovery reply: {reply!r}")
        return cls(ip=fields[1], name=fields[2], model=fields[3],
                   serial=fields[4], firmware=fields[5])

    def __str__(self) -> str:
        return (f"{self.model} '{self.name}' at {self.ip} "
                f"(serial {self.serial}, firmware {self.firmware})")


class _UDPProtocol(asyncio.DatagramProtocol):
    """Queues every received datagram for async consumption."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[bytes, tuple]] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.queue.put_nowait((data, addr))


class WingConsole:
    """Scans for and talks OSC to a Behringer WING console (asyncio).

    Create instances with the async factories `connect()` or `find()`,
    not by calling the constructor directly.
    """

    def __init__(self, ip: str, transport: asyncio.DatagramTransport,
                 protocol: _UDPProtocol, info: WingInfo | None = None,
                 timeout: float = 2.0):
        self.ip = ip
        self.info = info
        self.timeout = timeout
        self._transport = transport
        self._protocol = protocol
        self._request_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Discovery / connection
    # ------------------------------------------------------------------

    @staticmethod
    async def discover(broadcast: str = "255.255.255.255",
                       timeout: float = 2.0) -> list[WingInfo]:
        """Broadcast a discovery request and return all consoles that reply."""
        loop = asyncio.get_running_loop()
        protocol = _UDPProtocol()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol, local_addr=("0.0.0.0", 0), allow_broadcast=True)
        consoles: dict[str, WingInfo] = {}
        try:
            transport.sendto(DISCOVERY_REQUEST, (broadcast, DISCOVERY_PORT))
            deadline = loop.time() + timeout
            while (remaining := deadline - loop.time()) > 0:
                try:
                    data, _addr = await asyncio.wait_for(
                        protocol.queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                reply = data.decode("ascii", errors="replace").strip("\x00 \r\n")
                if reply.startswith(DISCOVERY_PREFIX):
                    try:
                        info = WingInfo.from_reply(reply)
                    except ValueError:
                        continue
                    consoles[info.ip] = info
        finally:
            transport.close()
        return list(consoles.values())

    @classmethod
    async def connect(cls, ip: str, info: WingInfo | None = None,
                      timeout: float = 2.0) -> "WingConsole":
        """Open an OSC connection to a console at a known IP."""
        loop = asyncio.get_running_loop()
        protocol = _UDPProtocol()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol, remote_addr=(ip, OSC_PORT))
        return cls(ip, transport, protocol, info=info, timeout=timeout)

    @classmethod
    async def find(cls, broadcast: str = "255.255.255.255",
                   timeout: float = 2.0) -> "WingConsole":
        """Discover consoles and connect to the first one found."""
        consoles = await cls.discover(broadcast=broadcast, timeout=timeout)
        if not consoles:
            raise RuntimeError(
                f"no WING console found (broadcast {broadcast}:{DISCOVERY_PORT})")
        info = consoles[0]
        return await cls.connect(info.ip, info=info, timeout=timeout)

    def close(self) -> None:
        self._transport.close()

    async def __aenter__(self) -> "WingConsole":
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # OSC I/O
    # ------------------------------------------------------------------

    def send(self, address: str, *args) -> None:
        """Send an OSC message without waiting for a reply."""
        builder = OscMessageBuilder(address=address)
        for arg in args:
            builder.add_arg(arg)
        self._transport.sendto(builder.build().dgram)

    async def receive(self) -> OscMessage:
        """Receive one OSC message (raises TimeoutError if none arrives)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while (remaining := deadline - loop.time()) > 0:
            data, _addr = await asyncio.wait_for(
                self._protocol.queue.get(), remaining)
            try:
                return OscMessage(data)
            except ParseError:
                continue  # ignore non-OSC datagrams (e.g. discovery replies)
        raise asyncio.TimeoutError(
            f"no OSC reply from {self.ip} within {self.timeout}s")

    async def request(self, address: str, *args) -> OscMessage:
        """Send an OSC message and return the console's reply."""
        async with self._request_lock:
            self.send(address, *args)
            return await self.receive()

    async def get(self, address: str):
        """Query the current value of a parameter node, e.g. '/ch/1/fdr'.

        The WING replies with up to three args: string form, normalized
        float, and raw value.
        """
        reply = await self.request(address)
        params = reply.params
        if len(params) == 1:
            return params[0]
        return params

    async def identify(self) -> WingInfo:
        """Ask the connected console for its identity via OSC ('/?')."""
        reply = await self.request("/?")
        info = WingInfo.from_reply(str(reply.params[0]))
        self.info = info
        return info


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan for and connect to a Behringer WING console")
    parser.add_argument("-b", "--broadcast", default="255.255.255.255",
                        help="broadcast address to scan (default: %(default)s)")
    parser.add_argument("-t", "--timeout", type=float, default=2.0,
                        help="discovery timeout in seconds (default: %(default)s)")
    args = parser.parse_args()

    print(f"Scanning for WING consoles on {args.broadcast}:{DISCOVERY_PORT} ...")
    consoles = await WingConsole.discover(broadcast=args.broadcast,
                                          timeout=args.timeout)
    if not consoles:
        print("No WING console found.")
        return 1

    for info in consoles:
        print(f"Found: {info}")

    info = consoles[0]
    print(f"\nConnecting to {info.ip} ...")
    async with await WingConsole.connect(info.ip, info=info,
                                         timeout=args.timeout) as wing:
        identity = await wing.identify()
        print(f"Connected. Console reports: {identity}")
        fdr = await wing.get("/ch/1/fdr")
        print(f"Example query — channel 1 fader: {fdr}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
