"""Windows WASAPI loopback and Android microphone bridge over USB/Wi-Fi ADB."""
import argparse
from dataclasses import dataclass
import contextlib
import ipaddress
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import re
from urllib.parse import urlsplit

from protocol import FRAMES, MIC_PORT, PLAY_PORT, RATE, make_header, recv_exact
from check_environment import check_audio_dependencies
from mic_sessions import DebouncedActivity, active_capture_processes
from udp_protocol import (
    ACK, ACK_MAGIC, CONTROL, CONTROL_MAGIC, CONTROL_PORT, FLAG_COMMUNICATION,
    FLAG_HIGH_QUALITY, FLAG_MICROPHONE, FLAG_STABLE, FragmentAssembler,
    JitterBuffer, UDP_FRAMES, UDP_PLAY_PORT, pack_audio, unpack_fragment,
    FLAG_STOP, FLAG_QUERY_CAPABILITIES,
)


QUALITY_PROFILES = {
    "standard": (48000, 16, "标准"),
    "high": (96000, 24, "中品质"),
    "lossless": (192000, 24, "无损品质"),
}


def float_to_pcm(block, bits):
    import numpy as np
    clipped = np.clip(block, -1.0, 1.0)
    if bits == 16:
        return (clipped * 32767).astype("<i2").tobytes()
    if bits == 24:
        values = np.ascontiguousarray((clipped * 8388607).astype("<i4")).ravel()
        return values.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    raise ValueError(f"Unsupported PCM bits: {bits}")


def resolve_speaker(soundcard, selector):
    """Resolve IDs literally; SoundCard treats strings as fuzzy/regex queries."""
    if selector is None:
        speaker = soundcard.default_speaker()
        if speaker is None:
            raise RuntimeError("No Windows playback device found.")
        return speaker
    devices = soundcard.all_speakers()
    exact_id = [item for item in devices if item.id == selector]
    if len(exact_id) == 1:
        return exact_id[0]
    exact_name = [item for item in devices if item.name.casefold() == selector.casefold()]
    if len(exact_name) == 1:
        return exact_name[0]
    partial = [item for item in devices if selector.casefold() in item.name.casefold()]
    if len(partial) == 1:
        return partial[0]
    raise RuntimeError(f"Playback device is missing or ambiguous: {selector}")


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    transport: str
    model: str


def resolve_adb_executable(adb="adb"):
    """Prefer the ADB shipped beside the application over PATH."""
    requested = Path(adb)
    if requested.name.lower() not in ("adb", "adb.exe") or requested.parent != Path("."):
        explicit = shutil.which(adb)
        if explicit:
            return explicit
        raise RuntimeError(f"未找到指定的 ADB：{adb}")

    application_dir = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                       else Path(__file__).resolve().parent.parent)
    candidates = (
        application_dir / "adb.exe",
        application_dir / "adb" / "adb.exe",
        Path.cwd() / "adb.exe",
        Path.cwd() / "adb" / "adb.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    executable = shutil.which(adb)
    if not executable:
        raise RuntimeError("未找到 ADB；发布包应将 adb.exe 与 PhoneAudioBridge.exe 放在同一目录。")
    return executable


def adb_command(adb, *args):
    executable = resolve_adb_executable(adb)
    try:
        return subprocess.check_output(
            [executable, *args], text=True, encoding="utf-8", errors="replace",
            stderr=subprocess.STDOUT, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError(error.output.strip()) from error


def parse_devices(listing):
    devices = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1] not in ("device", "offline", "unauthorized", "no"):
            continue
        serial, state = fields[:2]
        if serial.startswith("emulator-"):
            transport = "emulator"
        elif ":" in serial or "._adb-tls-connect._tcp" in serial:
            transport = "wifi"
        else:
            transport = "usb"
        model = next((field[6:] for field in fields[2:] if field.startswith("model:")), serial)
        devices.append(AdbDevice(serial, state, transport, model))
    return devices


def list_adb_devices(adb="adb"):
    return parse_devices(adb_command(adb, "devices", "-l"))


def select_device(devices, serial=None, transport="auto"):
    if transport not in ("auto", "usb", "wifi"):
        raise ValueError("Transport must be auto, usb or wifi")
    candidates = [item for item in devices if item.transport != "emulator"
                  and (transport == "auto" or item.transport == transport)
                  and (serial is None or item.serial == serial)]
    ready = [item for item in candidates if item.state == "device"]
    if not ready:
        states = ", ".join(f"{item.serial}: {item.state}" for item in candidates)
        raise RuntimeError("未发现在线手机。" + (states + "。" if states else "")
                           + "USB 请检查调试授权；Wi-Fi 请先连接无线 ADB，再刷新设备。")
    if serial is None and transport == "auto":
        usb = [item for item in ready if item.transport == "usb"]
        if len(usb) == 1:
            return usb[0]
    if len(ready) != 1:
        raise RuntimeError("存在多台在线手机，请在手机设备列表选择，或使用 --serial。")
    return ready[0]


def connect_wifi(address, adb="adb"):
    address = address.strip()
    try:
        parsed = urlsplit("//" + address)
        valid = (parsed.hostname and parsed.port and 1 <= parsed.port <= 65535
                 and not parsed.username and not parsed.password and not parsed.path
                 and not parsed.query and not parsed.fragment
                 and not any(char.isspace() for char in address))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("请输入无线调试连接地址，例如 192.168.1.10:5555。")
    return adb_command(adb, "connect", address)


def enable_wifi_adb(serial=None, adb="adb", port=5555):
    """Switch an approved USB device to legacy TCP ADB and connect to it."""
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("ADB TCP port must be between 1 and 65535")
    device = select_device(list_adb_devices(adb), serial, "usb")
    route = adb_command(
        adb, "-s", device.serial, "shell", "ip", "-4", "route", "get", "1.1.1.1")
    match = re.search(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})\b", route)
    if not match:
        address = adb_command(
            adb, "-s", device.serial, "shell", "ip", "-4", "addr", "show", "wlan0")
        match = re.search(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/", address)
    if not match:
        raise RuntimeError("手机没有可用的 WLAN IPv4，请先让手机连接 Wi-Fi。")
    phone_ip = ipaddress.ip_address(match.group(1))
    if phone_ip.version != 4 or phone_ip.is_loopback or phone_ip.is_unspecified:
        raise RuntimeError("手机 WLAN IPv4 无效。")
    adb_command(adb, "-s", device.serial, "tcpip", str(port))
    time.sleep(0.8)
    target = f"{phone_ip}:{port}"
    result = connect_wifi(target, adb)
    lowered = result.casefold()
    if "connected" not in lowered and "already connected" not in lowered:
        raise RuntimeError(result or f"无线 ADB 连接失败：{target}")
    return target


ANDROID_DEVICE_TYPES = {
    2: "内置扬声器",
    3: "有线耳麦",
    4: "有线耳机",
    7: "蓝牙 SCO",
    8: "蓝牙 A2DP",
    9: "HDMI",
    11: "USB 音频设备",
    22: "USB 耳麦",
    26: "BLE 耳麦",
}


def windows_audio_capabilities(selector=None):
    import soundcard as sc
    speaker = resolve_speaker(sc, selector)
    channels = int(speaker.channels)
    return {
        "name": speaker.name,
        "channels": channels,
        "layout": channel_layout(channels),
    }


def channel_layout(channels):
    return {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(channels, f"{channels} 声道")


def query_android_audio_capabilities(serial=None, adb="adb", transport="auto"):
    device = select_device(list_adb_devices(adb), serial, transport)
    resolve_adb_executable(adb)
    command = lambda *args: adb_command(adb, "-s", device.serial, *args)
    services = command("shell", "dumpsys", "activity", "services", "dev.usbaudio")
    if "dev.usbaudio/.AudioService" not in services:
        raise RuntimeError("请先在手机 Phone Audio Bridge 中点击“启动音频”。")
    local_port = int(command("forward", "tcp:0", f"tcp:{CONTROL_PORT}"))
    try:
        with socket.create_connection(("127.0.0.1", local_port), timeout=5) as control:
            control.sendall(CONTROL.pack(
                CONTROL_MAGIC, 0, 0, 48000, 1, UDP_FRAMES, 16,
                FLAG_QUERY_CAPABILITIES))
            (magic, _generation, current, maximum, device_type, actual_bits,
             max_bits, actual_rate, max_rate) = ACK.unpack(
                recv_exact(control, ACK.size))
            if magic != ACK_MAGIC:
                raise RuntimeError("手机能力响应无效。")
            return {
                "serial": device.serial,
                "device": ANDROID_DEVICE_TYPES.get(device_type, f"Android 设备类型 {device_type}"),
                "channels": current,
                "max_channels": maximum,
                "layout": channel_layout(current),
                "max_layout": channel_layout(maximum),
                "bits": actual_bits,
                "max_bits": max_bits,
                "rate": actual_rate,
                "max_rate": max_rate,
            }
    finally:
        with contextlib.suppress(RuntimeError, OSError):
            command("forward", "--remove", f"tcp:{local_port}")


class AdbForward:
    def __init__(self, serial=None, adb="adb", transport="auto"):
        self.adb = adb
        self.serial = serial
        self.transport = transport
        self.ports = {}

    def command(self, *args):
        return adb_command(self.adb, "-s", self.serial, *args)

    def __enter__(self):
        device = select_device(list_adb_devices(self.adb), self.serial, self.transport)
        self.serial = device.serial
        self.transport = device.transport
        services = self.command("shell", "dumpsys", "activity", "services", "dev.usbaudio")
        if "dev.usbaudio/.AudioService" not in services:
            raise RuntimeError("手机音频服务未启动：请打开手机 Phone Audio Bridge，允许麦克风权限并点击“启动音频”。")
        try:
            for remote in (PLAY_PORT, MIC_PORT):
                self.ports[remote] = int(self.command("forward", "tcp:0", f"tcp:{remote}"))
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def connect(self, remote, channels, communication=False, frames=FRAMES,
                rate=RATE, bits=16):
        sock = socket.create_connection(("127.0.0.1", self.ports[remote]), timeout=5)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                            max(8192, frames * channels * 2 * 4))
            sock.sendall(make_header(channels, communication, frames, rate, bits))
            return sock
        except BaseException:
            sock.close()
            raise

    def __exit__(self, *args):
        for port in self.ports.values():
            try:
                self.command("forward", "--remove", f"tcp:{port}")
            except (subprocess.SubprocessError, OSError, RuntimeError):
                pass
        self.ports.clear()


class LanUdpSession:
    def __init__(self, serial, adb, communication, microphone, stable, rate, bits):
        self.serial = serial
        self.adb = adb
        self.communication = communication
        self.microphone = microphone
        self.stable = stable
        self.rate = rate
        self.bits = bits
        self.token = secrets.randbits(64)
        self.control_port = None
        self.playback = None
        self.mic = None
        self.pc_ip = None

    def command(self, *args):
        return adb_command(self.adb, "-s", self.serial, *args)

    def __enter__(self):
        parsed = urlsplit("//" + self.serial)
        try:
            phone_ip = ipaddress.ip_address(parsed.hostname)
        except ValueError as error:
            raise RuntimeError("UDP 直连需要 IP:PORT 形式的无线 ADB 地址。") from error
        if phone_ip.version != 4:
            raise RuntimeError("当前 UDP 直连仅支持 IPv4 局域网。")
        services = self.command("shell", "dumpsys", "activity", "services", "dev.usbaudio")
        if "dev.usbaudio/.AudioService" not in services:
            raise RuntimeError("手机音频服务未启动：请在手机 Phone Audio Bridge 中点击“启动音频”。")
        try:
            self.playback = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.playback.connect((str(phone_ip), UDP_PLAY_PORT))
            pc_ip = self.playback.getsockname()[0]
            self.pc_ip = pc_ip
            self.mic = self.playback
            self.mic.settimeout(0.25)
            flags = ((FLAG_COMMUNICATION if self.communication else 0)
                     | (FLAG_MICROPHONE if self.microphone else 0)
                     | (FLAG_STABLE if self.stable else 0)
                     | (FLAG_HIGH_QUALITY if self.bits == 24 else 0))
            self.control_port = int(self.command(
                "forward", "tcp:0", f"tcp:{CONTROL_PORT}"))
            with socket.create_connection(("127.0.0.1", self.control_port), timeout=5) as control:
                control.sendall(CONTROL.pack(
                    CONTROL_MAGIC, self.token, int(ipaddress.IPv4Address(pc_ip)), self.rate,
                    self.mic.getsockname()[1], self.rate // 200, self.bits, flags))
                response = recv_exact(control, ACK.size)
                (magic, _generation, _current, _maximum, _device_type, actual_bits,
                 _max_bits, actual_rate, _max_rate) = ACK.unpack(response)
                if magic != ACK_MAGIC:
                    raise RuntimeError("手机拒绝 UDP 音频会话。")
                self.rate = actual_rate
                self.bits = actual_bits
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.control_port is not None and self.pc_ip is not None and self.mic is not None:
            with contextlib.suppress(OSError, RuntimeError, EOFError):
                with socket.create_connection(("127.0.0.1", self.control_port), timeout=2) as control:
                    control.sendall(CONTROL.pack(
                        CONTROL_MAGIC, self.token, int(ipaddress.IPv4Address(self.pc_ip)), 48000,
                        self.mic.getsockname()[1], UDP_FRAMES, 16, FLAG_STOP))
                    recv_exact(control, ACK.size)
        for sock in {self.playback, self.mic}:
            if sock is not None:
                sock.close()
        if self.control_port is not None:
            with contextlib.suppress(RuntimeError, OSError):
                self.command("forward", "--remove", f"tcp:{self.control_port}")
        self.playback = None
        self.mic = None
        self.control_port = None
        self.pc_ip = None


class Bridge:
    def __init__(self, capture_speaker=None, mic_speaker=None, serial=None, adb="adb", log=print,
                 transport="auto", playback_mode="auto", latency_profile="low",
                 quality_profile="standard"):
        if playback_mode not in ("auto", "media", "communication", "follow"):
            raise ValueError("Playback mode must be auto, media, communication or follow")
        if latency_profile not in ("low", "stable"):
            raise ValueError("Latency profile must be low or stable")
        if quality_profile not in QUALITY_PROFILES:
            raise ValueError("Unknown quality profile")
        self.capture_speaker = capture_speaker
        self.mic_speaker = mic_speaker
        self.serial = serial
        self.adb = adb
        self.transport = transport
        self.playback_mode = playback_mode
        self.latency_profile = latency_profile
        self.frames = 240 if latency_profile == "low" else 480
        self.quality_profile = quality_profile
        self.log = log
        self.stop_event = threading.Event()
        self.sockets = []
        self.socket_lock = threading.Lock()
        self.play_frames = 0
        self.mic_frames = 0
        self.errors = []
        self.mic_db = -120.0
        self.child_bridge = None

    def test_microphone(self, seconds=2):
        """Measure phone input without storing audio or requiring a Windows sink."""
        check_audio_dependencies()
        import numpy as np
        energy = 0.0
        samples = 0
        peak = 0.0
        try:
            with AdbForward(self.serial, self.adb, self.transport) as adb:
                self.log(f"手机: {adb.serial} ({adb.transport})；请对手机说话")
                with adb.connect(MIC_PORT, 1) as sock:
                    with self.socket_lock:
                        self.sockets.append(sock)
                    for _ in range(round(seconds * RATE / FRAMES)):
                        if self.stop_event.is_set():
                            return None
                        pcm = recv_exact(sock, FRAMES * 2)
                        block = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
                        energy += float(np.sum(block * block))
                        peak = max(peak, float(np.max(np.abs(block))))
                        samples += len(block)
            db = 20 * np.log10(max((energy / max(samples, 1)) ** 0.5, 1e-6))
            self.log(f"手机麦克风: RMS {db:.1f} dBFS | Peak {peak:.3f}")
            self.log("手机录音有信号；软件输入需选择虚拟线录制端。" if peak > 0
                     else "手机返回静音；检查手机麦克风静音开关、录音权限和系统麦克风开关。")
            return {"samples": samples, "rms_db": float(db), "peak": peak}
        finally:
            with self.socket_lock:
                self.sockets.clear()

    def stop(self):
        self.stop_event.set()
        if self.child_bridge is not None:
            self.child_bridge.stop()
        with self.socket_lock:
            for sock in self.sockets:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    def run(self):
        if self.playback_mode == "follow":
            if not self.mic_speaker:
                raise ValueError("跟随 Windows 麦克风需要先选择 CABLE Input 作为麦克风接收设备。")
            return self.run_following_microphone()
        return self.run_once()

    def run_following_microphone(self):
        state = DebouncedActivity(initial=bool(active_capture_processes()))
        current_mode = "communication" if state.value else "media"
        self.log("Windows 麦克风跟随: " + ("通话" if state.value else "影音"))
        while not self.stop_event.is_set():
            child_error = []
            child = Bridge(
                self.capture_speaker, self.mic_speaker, self.serial, self.adb, self.log,
                self.transport, current_mode, self.latency_profile, self.quality_profile)
            self.child_bridge = child

            def work():
                try:
                    child.run_once()
                except Exception as error:
                    child_error.append(error)

            worker = threading.Thread(target=work, name="audio-mode-session")
            worker.start()
            desired = current_mode
            last_processes = None
            while worker.is_alive() and not self.stop_event.wait(0.25):
                processes = active_capture_processes()
                if processes != last_processes:
                    self.log("Windows 麦克风使用: "
                             + (", ".join(processes) if processes else "空闲"))
                    last_processes = processes
                if state.update(bool(processes)):
                    desired = "communication" if state.value else "media"
                    names = ", ".join(processes) if processes else "无活动程序"
                    self.log(f"Windows 麦克风: {names}；切换到"
                             + ("通话模式" if state.value else "影音模式"))
                    child.stop()
                    break
            if self.stop_event.is_set():
                child.stop()
            worker.join()
            self.child_bridge = None
            if child_error and desired == current_mode and not self.stop_event.is_set():
                raise child_error[0]
            current_mode = desired
        self.log("麦克风会话监控已停止。")

    def run_once(self):
        check_audio_dependencies()
        import numpy as np
        import soundcard as sc

        capture = resolve_speaker(sc, self.capture_speaker)
        loopback = next((item for item in sc.all_microphones(include_loopback=True)
                         if item.id == capture.id and item.isloopback), None)
        if loopback is None:
            raise RuntimeError("所选播放设备没有可用的 WASAPI Loopback 端点。")
        sink = resolve_speaker(sc, self.mic_speaker) if self.mic_speaker else None
        if sink and sink.id == capture.id:
            raise ValueError("Microphone output and loopback capture must be different devices (feedback loop).")
        self.log(f"Capture: {capture.name}")
        self.log(f"Microphone output: {sink.name if sink else 'disabled'}")
        communication = (self.playback_mode == "communication"
                         or (self.playback_mode == "auto" and sink is not None))
        selected = select_device(list_adb_devices(self.adb), self.serial, self.transport)
        capabilities = query_android_audio_capabilities(
            selected.serial, self.adb, selected.transport)
        requested_rate, requested_bits, quality_name = QUALITY_PROFILES[self.quality_profile]
        if communication:
            requested_rate, requested_bits, quality_name = QUALITY_PROFILES["standard"]
        effective_rate = max(rate for rate in (48000, 96000, 192000)
                             if rate <= min(requested_rate, capabilities["max_rate"]))
        effective_bits = 24 if requested_bits == 24 and capabilities["max_bits"] >= 24 else 16
        play_frames = effective_rate // (100 if self.latency_profile == "stable" else 200)
        mode_name = "通话（AEC）" if communication else "影音"
        self.log(f"Requested mode: {mode_name}"
                 + ("（自动）" if self.playback_mode == "auto" else "（手动）"))
        self.log(f"Latency: {'5 ms 低延迟' if self.frames == 240 else '10 ms 稳定'}")
        fallback = "（已回退）" if (effective_rate, effective_bits) != (requested_rate, requested_bits) else ""
        self.log(f"Quality: {quality_name} | {effective_rate // 1000} kHz / {effective_bits}-bit {fallback}")
        if sink is None:
            self.log("麦克风回传已关闭；需要回传时请选择虚拟音频线的播放端。")

        def downlink(sock):
            io_frames = effective_rate // 100
            with loopback.recorder(samplerate=effective_rate, channels=2,
                                   blocksize=io_frames * 2) as recorder:
                while not self.stop_event.is_set():
                    block = recorder.record(numframes=io_frames)
                    pcm = float_to_pcm(block, effective_bits)
                    sock.sendall(pcm)
                    self.play_frames += io_frames * RATE / effective_rate

        def uplink(sock):
            with sink.player(samplerate=RATE, channels=2, blocksize=FRAMES * 2) as player:
                while not self.stop_event.is_set():
                    io_frames = max(self.frames, 480)
                    pcm = recv_exact(sock, io_frames * 2)
                    mono = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                    self.mic_db = float(20 * np.log10(max(float(np.sqrt(np.mean(mono * mono))), 1e-6)))
                    player.play(np.repeat(mono[:, None], 2, axis=1))
                    self.mic_frames += io_frames

        def udp_downlink(session):
            sequence = 0
            packet_frames = session.rate // 200
            with loopback.recorder(samplerate=session.rate, channels=2,
                                   blocksize=packet_frames * 4) as recorder:
                while not self.stop_event.is_set():
                    captured = recorder.record(numframes=packet_frames * 2)
                    pcm = float_to_pcm(captured, session.bits)
                    frame_bytes = packet_frames * 2 * (session.bits // 8)
                    for offset in range(0, len(pcm), frame_bytes):
                        payload = pcm[offset:offset + frame_bytes]
                        packets = pack_audio(
                            session.token, sequence, time.monotonic_ns() // 1000,
                            2, payload, session.rate, session.bits)
                        for packet in packets:
                            session.playback.send(packet)
                        sequence += 1
                        self.play_frames += packet_frames * 48000 / session.rate

        def udp_uplink(session):
            jitter = JitterBuffer(4 if self.latency_profile == "stable" else 2)
            assembler = FragmentAssembler()
            pending = bytearray()
            with sink.player(samplerate=RATE, channels=2, blocksize=FRAMES * 2) as player:
                while not self.stop_event.is_set():
                    try:
                        packet = session.mic.recv(2048)
                    except socket.timeout:
                        continue
                    try:
                        sequence, _timestamp, fragment, count, payload = unpack_fragment(
                            packet, session.token, 1, 48000, 16)
                    except ValueError:
                        continue
                    pcm = assembler.push(sequence, fragment, count, payload)
                    if pcm is None:
                        continue
                    for ordered in jitter.push(sequence, pcm):
                        pending.extend(ordered)
                    while len(pending) >= UDP_FRAMES * 2 * 2:
                        pcm = bytes(pending[:UDP_FRAMES * 2 * 2])
                        del pending[:UDP_FRAMES * 2 * 2]
                        mono = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                        self.mic_db = float(20 * np.log10(
                            max(float(np.sqrt(np.mean(mono * mono))), 1e-6)))
                        player.play(np.repeat(mono[:, None], 2, axis=1))
                        self.mic_frames += len(mono)

        def worker(fn, sock):
            try:
                fn(sock)
            except Exception as error:
                if not self.stop_event.is_set():
                    if "0x8889000a" in str(error).lower():
                        friendly = RuntimeError(
                            "Windows 音频设备被其他程序独占（0x8889000a）。"
                            "请退出使用该设备的软件，或在 mmsys.cpl 的设备属性“高级”中"
                            "取消“允许应用程序独占控制此设备”，应用后重试。"
                        )
                        friendly.__cause__ = error
                        error = friendly
                    self.errors.append(error)
                    self.log(f"Audio error: {error}")
            finally:
                self.stop()

        if selected.transport == "wifi":
            with LanUdpSession(
                    selected.serial, self.adb, communication, sink is not None,
                    self.latency_profile == "stable", effective_rate,
                    effective_bits) as udp:
                with self.socket_lock:
                    self.sockets.extend([udp.playback, udp.mic])
                jobs = [threading.Thread(target=worker, args=(udp_downlink, udp),
                                         name="udp-downlink")]
                if sink:
                    jobs.append(threading.Thread(target=worker, args=(udp_uplink, udp),
                                                 name="udp-uplink"))
                try:
                    for job in jobs:
                        job.start()
                    self.log(f"手机: {selected.serial} (Wi-Fi UDP 直连)")
                    self.log("UDP audio running. Stop with Ctrl+C or the Stop button.")
                    while not self.stop_event.wait(1):
                        self.log(f"Playback {self.play_frames / RATE:.0f}s | Microphone {self.mic_frames / RATE:.0f}s"
                                 + (f" | Mic {self.mic_db:.1f} dBFS" if sink else ""))
                finally:
                    self.stop()
                    for job in jobs:
                        job.join()
                    with self.socket_lock:
                        self.sockets.clear()
            if self.errors:
                raise RuntimeError(str(self.errors[0])) from self.errors[0]
            self.log("Stopped; UDP session and ADB control forward removed.")
            return

        with AdbForward(selected.serial, self.adb, "usb") as usb, contextlib.ExitStack() as stack:
            self.log(f"手机: {usb.serial} ({usb.transport})")
            jobs = []
            for remote, channels, communication, fn in [
                    (PLAY_PORT, 2, communication, downlink)] + (
                    [(MIC_PORT, 1, communication, uplink)] if sink else []):
                if self.stop_event.is_set():
                    break
                sock = stack.enter_context(usb.connect(
                    remote, channels, communication,
                    play_frames if remote == PLAY_PORT else self.frames,
                    effective_rate if remote == PLAY_PORT else RATE,
                    effective_bits if remote == PLAY_PORT else 16))
                with self.socket_lock:
                    self.sockets.append(sock)
                jobs.append(threading.Thread(target=worker, args=(fn, sock), name=fn.__name__))
            try:
                for job in jobs:
                    job.start()
                if jobs:
                    self.log("ADB audio running. Stop with Ctrl+C or the Stop button.")
                    while not self.stop_event.wait(1):
                        self.log(f"Playback {self.play_frames / RATE:.0f}s | Microphone {self.mic_frames / RATE:.0f}s"
                                 + (f" | Mic {self.mic_db:.1f} dBFS" if sink else ""))
            finally:
                self.stop()
                for job in jobs:
                    if job.ident is not None:
                        job.join()
                with self.socket_lock:
                    self.sockets.clear()
        if self.errors:
            raise RuntimeError(str(self.errors[0])) from self.errors[0]
        self.log("Stopped; ADB forwards removed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List Windows playback devices")
    parser.add_argument("--capture-speaker", help="Device ID or unique name; default is system playback")
    parser.add_argument("--mic-speaker", help="Virtual cable playback endpoint; omit to disable microphone")
    parser.add_argument("--serial", help="ADB USB serial")
    parser.add_argument("--adb", default="adb", help="ADB executable path")
    parser.add_argument("--transport", choices=("auto", "usb", "wifi"), default="auto")
    parser.add_argument("--devices", action="store_true", help="List ADB phones")
    parser.add_argument("--connect", metavar="HOST:PORT", help="Connect Wi-Fi ADB before streaming")
    parser.add_argument("--test-mic", action="store_true", help="Measure phone microphone for 2 seconds")
    parser.add_argument("--mode", choices=("auto", "media", "communication", "follow"), default="auto",
                        help="Android playback processing mode")
    parser.add_argument("--latency", choices=("low", "stable"), default="low")
    parser.add_argument("--quality", choices=tuple(QUALITY_PROFILES), default="standard")
    args = parser.parse_args()
    if args.list:
        import soundcard as sc
        default = sc.default_speaker()
        for speaker in sc.all_speakers():
            print(f"{'*' if default and default.id == speaker.id else ' '} {speaker.name}\n  {speaker.id}")
        return
    bridge = Bridge(args.capture_speaker, args.mic_speaker, args.serial, args.adb,
                    transport=args.transport, playback_mode=args.mode,
                    latency_profile=args.latency, quality_profile=args.quality)
    try:
        if args.connect:
            print(connect_wifi(args.connect, args.adb))
        if args.devices:
            for device in list_adb_devices(args.adb):
                print(f"{device.serial} | {device.transport} | {device.state} | {device.model}")
        elif args.test_mic:
            bridge.test_microphone()
        else:
            bridge.run()
    except KeyboardInterrupt:
        bridge.stop()
    except Exception as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
