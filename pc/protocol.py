"""USB Audio v1: 16-byte network-order header followed by little-endian PCM."""
import socket
import struct

RATE = 48000
FRAMES = 480
SUPPORTED_RATES = (48000, 96000, 192000)
SUPPORTED_BITS = (16, 24)
PLAY_PORT = 27183
MIC_PORT = 27184
HEADER = struct.Struct("!4sIHHI")


def make_header(channels: int, communication: bool = False, frames: int = FRAMES,
                rate: int = RATE, bits: int = 16) -> bytes:
    if channels not in (1, 2):
        raise ValueError("Channels must be 1 or 2")
    if rate not in SUPPORTED_RATES or bits not in SUPPORTED_BITS:
        raise ValueError("Unsupported PCM format")
    if frames not in (rate // 200, rate // 100):
        raise ValueError(f"Unsupported frame count: {frames}")
    frame_flags = frames | (0x80000000 if communication else 0)
    return HEADER.pack(b"UAB1", rate, channels, bits, frame_flags)


def validate_header(data: bytes, channels: int, communication: bool = False,
                    frames: int = FRAMES, rate: int = RATE, bits: int = 16) -> None:
    if data != make_header(channels, communication, frames, rate, bits):
        raise ValueError("Unsupported audio header")


def recv_exact(sock: socket.socket, size: int) -> bytes:
    if size < 0:
        raise ValueError("Size must be nonnegative")
    data = bytearray(size)
    view = memoryview(data)
    offset = 0
    while offset < size:
        count = sock.recv_into(view[offset:])
        if count == 0:
            raise EOFError("手机音频通道已断开，请检查手机音频服务和 ADB 连接。")
        offset += count
    return bytes(data)
