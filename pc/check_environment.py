"""Exercise audio dependencies; package metadata alone misses deleted files."""
import argparse
import importlib.util
import sys


def check_audio_dependencies():
    try:
        import numpy as np
        from cffi import FFI
        import soundcard as sc

        if not np.__version__.startswith("1."):
            raise RuntimeError("SoundCard 0.4.5 requires NumPy 1.x in this project")
        block = np.zeros((480, 2), dtype=np.float32)
        pcm = (np.clip(block, -1, 1) * 32767).astype("<i2").tobytes()
        restored = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        stereo = np.repeat(restored[:480, None], 2, axis=1)
        if stereo.shape != (480, 2) or float(np.sqrt(np.mean(stereo * stereo))) != 0:
            raise RuntimeError("NumPy PCM conversion failed")
        ffi = FFI()
        ffi.cdef("struct AudioSample { short value; };")
        ffi.new("struct AudioSample *")
        if not callable(sc.all_speakers) or not callable(sc.all_microphones):
            raise RuntimeError("SoundCard audio APIs are missing")
        if importlib.util.find_spec("pycaw.pycaw") is None or importlib.util.find_spec("comtypes") is None:
            raise RuntimeError("pycaw session modules are missing")
        return np.__version__
    except Exception as error:
        raise RuntimeError(
            "音频依赖损坏或版本不兼容，请运行 start-pc.ps1 修复环境。"
            f" Details: {error}"
        ) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    try:
        version = check_audio_dependencies()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"Audio dependency check passed: NumPy {version}, CFFI, SoundCard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
