"""Windows endpoint format/mute management, scoped to one exact endpoint ID.

PolicyConfig ABI reference: tartakynov/audioswitch, IPolicyConfig.h.
This Windows interface is undocumented: always read back and restore on failure.
No COM pointers cross threads; every operation owns and releases its apartment refs.
"""
import contextlib
import ctypes as ct
import struct
import uuid


class GUID(ct.Structure):
    _fields_ = [("data", ct.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__()
        self.data[:] = uuid.UUID(value).bytes_le


def describe_format(data):
    tag, channels, rate, _, align, bits, extra = struct.unpack_from("<HHIIHHH", data)
    valid = struct.unpack_from("<H", data, 18)[0] if tag == 65534 and extra >= 22 else bits
    subtag = struct.unpack_from("<I", data, 24)[0] if tag == 65534 and extra >= 22 else tag
    return {"channels": channels, "rate": rate, "bits": valid or bits,
            "container_bits": bits, "float": subtag == 3, "align": align}


def pcm_format(original, rate, bits, container_bits=None):
    channels = describe_format(original)["channels"]
    mask = struct.unpack_from("<I", original, 20)[0] if len(original) >= 40 else (3 if channels == 2 else 4)
    container_bits = container_bits or bits
    align = channels * (container_bits // 8)
    return (struct.pack("<HHIIHHHHI", 65534, channels, rate, rate * align, align, container_bits, 22, bits, mask)
            + uuid.UUID("00000001-0000-0010-8000-00aa00389b71").bytes_le)


def _check(hr):
    if hr < 0:
        raise RuntimeError(f"Windows 音频接口错误 0x{hr & 0xffffffff:08x}")


class ComScope:
    def __enter__(self):
        self.ole = ct.OleDLL("ole32")
        self.ole.CoInitializeEx.restype = ct.c_long
        hr = self.ole.CoInitializeEx(None, 0)
        self.initialized = hr >= 0
        if hr < 0 and (hr & 0xffffffff) != 0x80010106:
            _check(hr)
        self.refs = []
        return self

    def call(self, pointer, index, types, *args):
        table = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
        function = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, *types)(table[index])
        hr = function(pointer, *args)
        _check(hr)
        return hr

    def create(self, clsid, iid):
        pointer = ct.c_void_p()
        self.ole.CoCreateInstance.argtypes = [ct.POINTER(GUID), ct.c_void_p, ct.c_uint32,
                                              ct.POINTER(GUID), ct.POINTER(ct.c_void_p)]
        self.ole.CoCreateInstance.restype = ct.c_long
        _check(self.ole.CoCreateInstance(ct.byref(GUID(clsid)), None, 1,
                                         ct.byref(GUID(iid)), ct.byref(pointer)))
        self.refs.append(pointer)
        return pointer

    def policy(self):
        return self.create("870af99c-171d-4f9e-af0d-e63df40c2bc9",
                           "f8679f50-850a-41cf-9c72-430f290290c8")

    def volume(self, device_id):
        enum = self.create("bcde0395-e52f-467c-8e3d-c4579291692e",
                           "a95664d2-9614-4f35-a746-de8db63617e6")
        device = ct.c_void_p()
        self.call(enum, 5, [ct.c_wchar_p, ct.POINTER(ct.c_void_p)], device_id, ct.byref(device))
        self.refs.append(device)
        volume = ct.c_void_p()
        self.call(device, 3, [ct.POINTER(GUID), ct.c_uint32, ct.c_void_p, ct.POINTER(ct.c_void_p)],
                  ct.byref(GUID("5cdf2c82-841e-4546-9722-0cf74078229a")), 23, None, ct.byref(volume))
        self.refs.append(volume)
        return volume

    def __exit__(self, *args):
        for pointer in reversed(self.refs):
            self.call(pointer, 2, [])
        if self.initialized:
            self.ole.CoUninitialize()


def get_format(device_id, mix=False):
    with ComScope() as scope:
        policy = scope.policy()
        pointer = ct.c_void_p()
        if mix:
            scope.call(policy, 3, [ct.c_wchar_p, ct.POINTER(ct.c_void_p)], device_id, ct.byref(pointer))
        else:
            scope.call(policy, 4, [ct.c_wchar_p, ct.c_int, ct.POINTER(ct.c_void_p)],
                       device_id, 0, ct.byref(pointer))
        try:
            base = ct.string_at(pointer, 18)
            size = 18 + struct.unpack_from("<H", base, 16)[0]
            if size > 4096:
                raise RuntimeError("Windows 返回了异常的音频格式长度")
            return ct.string_at(pointer, size)
        finally:
            scope.ole.CoTaskMemFree.argtypes = [ct.c_void_p]
            scope.ole.CoTaskMemFree(pointer)


def set_format(device_id, data, mix_data=None):
    with ComScope() as scope:
        device = ct.create_string_buffer(data)
        mix = ct.create_string_buffer(mix_data) if mix_data else None
        scope.call(scope.policy(), 6, [ct.c_wchar_p, ct.c_void_p, ct.c_void_p], device_id, device, mix)


def get_mute(device_id):
    with ComScope() as scope:
        value = ct.c_int()
        scope.call(scope.volume(device_id), 15, [ct.POINTER(ct.c_int)], ct.byref(value))
        return bool(value.value)


def set_mute(device_id, mute):
    with ComScope() as scope:
        scope.call(scope.volume(device_id), 14, [ct.c_int, ct.c_void_p], int(mute), None)


@contextlib.contextmanager
def endpoint_settings(device_id, rate, bits, phone_only=False, log=print, change_format=True):
    original = get_format(device_id)
    original_mix = get_format(device_id, mix=True)
    original_mute = get_mute(device_id)
    changed_format = False
    changed_mute = False
    try:
        current = describe_format(original)
        if change_format and (current["rate"], current["bits"], current["float"]) != (rate, bits, False):
            changed_format = True  # Roll back even if the driver partially applies then fails.
            try:
                try:
                    set_format(device_id, pcm_format(original, rate, bits))
                except RuntimeError:
                    if bits != 24:
                        raise
                    set_format(device_id, pcm_format(original, rate, bits, 32))
                current = describe_format(get_format(device_id))
                if (current["rate"], current["bits"], current["float"]) != (rate, bits, False):
                    raise RuntimeError(f"驱动读回格式为 {current}")
            except Exception as error:
                raise RuntimeError(f"扬声器拒绝 {rate} Hz / {bits}-bit；请选择较低品质。{error}") from error
        log(f"Windows 默认格式已确认: {current['rate']} Hz / {current['bits']}-bit PCM")
        mix = describe_format(get_format(device_id, mix=True))
        if change_format and mix["rate"] != rate:
            raise RuntimeError(f"WASAPI 混音仍为 {mix['rate']} Hz，请关闭占用端点的软件后重试。")
        log(f"WASAPI 混音: {mix['rate']} Hz / {mix['bits']}-bit "
            + ("float" if mix['float'] else "PCM"))
        if phone_only and not original_mute:
            changed_mute = True
            set_mute(device_id, True)
            if not get_mute(device_id):
                raise RuntimeError("电脑播放端点静音未生效")
        yield current
    finally:
        failures = []
        if changed_format:
            try:
                set_format(device_id, original, original_mix)
                if get_format(device_id) != original:
                    raise RuntimeError("恢复后的格式与原格式不同")
            except Exception as error:
                failures.append(f"格式恢复失败：{error}")
        if changed_mute:
            try:
                set_mute(device_id, original_mute)
            except Exception as error:
                failures.append(f"静音恢复失败：{error}")
        if failures:
            log("；".join(failures))
            raise RuntimeError("；".join(failures))
        log("Windows 原始格式与静音状态已恢复。")
