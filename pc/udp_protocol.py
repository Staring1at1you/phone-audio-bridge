"""Authenticated LAN UDP audio protocol."""
import struct

UDP_MAGIC = b"UAU1"
CONTROL_MAGIC = b"UAC1"
ACK_MAGIC = b"UAKA"
UDP_PLAY_PORT = 27185
CONTROL_PORT = 27187
UDP_FRAMES = 240
MAX_FRAGMENT_PAYLOAD = 1200
PACKET = struct.Struct("!4sQIIIHHHHH")
CONTROL = struct.Struct("!4sQIIHHHI")
ACK = struct.Struct("!4sIHHIHHII")
FLAG_COMMUNICATION = 1
FLAG_MICROPHONE = 2
FLAG_STABLE = 4
FLAG_STOP = 8
FLAG_QUERY_CAPABILITIES = 16
FLAG_HIGH_QUALITY = 32


def pack_audio(token, sequence, timestamp_us, channels, pcm, rate=48000, bits=16):
    frames = rate // 200
    bytes_per_sample = bits // 8
    expected = frames * channels * bytes_per_sample
    if channels not in (1, 2) or bits not in (16, 24) or len(pcm) != expected:
        raise ValueError("Invalid UDP PCM payload")
    count = (len(pcm) + MAX_FRAGMENT_PAYLOAD - 1) // MAX_FRAGMENT_PAYLOAD
    return [PACKET.pack(
        UDP_MAGIC, token, sequence & 0xFFFFFFFF, timestamp_us & 0xFFFFFFFF,
        rate, channels, bits, frames, index, count,
    ) + pcm[index * MAX_FRAGMENT_PAYLOAD:(index + 1) * MAX_FRAGMENT_PAYLOAD]
            for index in range(count)]


def unpack_fragment(packet, token, channels, rate=48000, bits=16):
    if len(packet) < PACKET.size:
        raise ValueError("Short UDP packet")
    (magic, packet_token, sequence, timestamp_us, packet_rate, packet_channels,
     packet_bits, frames, fragment, fragment_count) = PACKET.unpack_from(packet)
    pcm = packet[PACKET.size:]
    expected_bytes = frames * channels * (bits // 8)
    expected_count = (expected_bytes + MAX_FRAGMENT_PAYLOAD - 1) // MAX_FRAGMENT_PAYLOAD
    expected_fragment_bytes = (MAX_FRAGMENT_PAYLOAD if fragment < expected_count - 1
                               else expected_bytes - MAX_FRAGMENT_PAYLOAD * (expected_count - 1))
    if (magic != UDP_MAGIC or packet_token != token or packet_channels != channels
            or packet_rate != rate or packet_bits != bits or frames != rate // 200
            or fragment >= fragment_count or fragment_count != expected_count
            or len(pcm) != expected_fragment_bytes):
        raise ValueError("Invalid UDP audio packet")
    return sequence, timestamp_us, fragment, fragment_count, pcm


class FragmentAssembler:
    def __init__(self, max_pending=8):
        self.max_pending = max_pending
        self.pending = {}

    def push(self, sequence, index, count, payload):
        fragments = self.pending.setdefault(sequence, [None] * count)
        if len(fragments) != count:
            self.pending.pop(sequence, None)
            return None
        fragments[index] = payload
        while len(self.pending) > self.max_pending:
            self.pending.pop(min(self.pending), None)
        if all(fragment is not None for fragment in fragments):
            self.pending.pop(sequence, None)
            return b"".join(fragments)
        return None


class JitterBuffer:
    def __init__(self, target_packets=2):
        if target_packets not in (2, 4):
            raise ValueError("Jitter target must be 2 or 4 packets")
        self.target = target_packets
        self.maximum = target_packets + 2
        self.packets = {}
        self.expected = None
        self.started = False
        self.dropped = 0

    def push(self, sequence, payload):
        if self.expected is not None and sequence < self.expected:
            self.dropped += 1
            return []
        self.packets.setdefault(sequence, payload)
        if not self.started:
            if len(self.packets) < self.target:
                return []
            self.expected = min(self.packets)
            self.started = True
        if self.expected not in self.packets and len(self.packets) >= self.maximum:
            next_sequence = min(self.packets)
            self.dropped += max(0, next_sequence - self.expected)
            self.expected = next_sequence
        output = []
        while self.expected in self.packets:
            output.append(self.packets.pop(self.expected))
            self.expected += 1
        return output
