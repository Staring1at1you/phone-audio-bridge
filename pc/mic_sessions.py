"""Monitor active Windows capture sessions on a named recording endpoint."""
import time
import os
import winreg


PRIVACY_KEY = (r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
               r"\ConsentStore\microphone\NonPackaged")


def active_privacy_processes():
    """Return desktop apps Windows currently marks as using a microphone."""
    processes = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, PRIVACY_KEY)
    except FileNotFoundError:
        return ()
    with root:
        for index in range(winreg.QueryInfoKey(root)[0]):
            name = winreg.EnumKey(root, index)
            try:
                with winreg.OpenKey(root, name) as item:
                    started = winreg.QueryValueEx(item, "LastUsedTimeStart")[0]
                    stopped = winreg.QueryValueEx(item, "LastUsedTimeStop")[0]
            except (FileNotFoundError, OSError):
                continue
            if started and (not stopped or started > stopped):
                processes.append(os.path.basename(name.replace("#", os.sep)))
    return tuple(sorted(set(processes)))


def active_capture_processes(device_name="CABLE Output"):
    import comtypes
    from pycaw.pycaw import (
        AudioSession,
        AudioUtilities,
        IAudioSessionControl2,
        IAudioSessionManager2,
    )

    comtypes.CoInitialize()
    try:
        matches = [device for device in AudioUtilities.GetAllDevices()
                   if device.FriendlyName and device_name.casefold() in device.FriendlyName.casefold()]
        if len(matches) != 1:
            names = ", ".join(device.FriendlyName for device in matches) or "none"
            raise RuntimeError(f"录制端点 {device_name!r} 匹配数量不是 1：{names}")
        device = matches[0]
        interface = device._dev.Activate(
            IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
        manager = interface.QueryInterface(IAudioSessionManager2)
        sessions = manager.GetSessionEnumerator()
        processes = []
        for index in range(sessions.GetCount()):
            control = sessions.GetSession(index).QueryInterface(IAudioSessionControl2)
            session = AudioSession(control)
            if session.State != 1 or session.ProcessId == 0:
                continue
            process = session.Process
            processes.append(process.name() if process else f"PID {session.ProcessId}")
        return tuple(sorted(set(processes) | set(active_privacy_processes())))
    finally:
        comtypes.CoUninitialize()


class DebouncedActivity:
    """Switch on quickly and switch off slowly to avoid reconnect thrashing."""
    def __init__(self, activate_delay=0.5, deactivate_delay=2.0, initial=False):
        self.value = initial
        self.candidate = initial
        self.changed_at = time.monotonic()
        self.activate_delay = activate_delay
        self.deactivate_delay = deactivate_delay

    def update(self, observed, now=None):
        now = time.monotonic() if now is None else now
        if observed != self.candidate:
            self.candidate = observed
            self.changed_at = now
        delay = self.activate_delay if self.candidate else self.deactivate_delay
        if self.value != self.candidate and now - self.changed_at >= delay:
            self.value = self.candidate
            return True
        return False
