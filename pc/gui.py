"""Desktop controls; audio and ADB work stay off the UI thread."""
import queue
import sys
import traceback
from pathlib import Path
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from bridge import (
    Bridge, channel_layout, enable_wifi_adb, list_adb_devices,
    query_android_audio_capabilities, windows_audio_capabilities,
)


class App:
    def __init__(self, root):
        self.root = root
        self.bridge = None
        self.thread = None
        self.events = queue.Queue()
        self.devices = []
        self.phones = []
        self.network_busy = False
        self.closing = False
        root.title("PhoneAudioBridge")
        root.geometry("720x610")
        root.minsize(600, 550)
        root.protocol("WM_DELETE_WINDOW", self.close)
        content = ttk.Frame(root, padding=24)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(13, weight=1)
        ttk.Label(content, text="PhoneAudioBridge", font=("Segoe UI", 22)).grid(row=0, sticky="w", pady=(0, 20))
        ttk.Label(content, text="手机设备 · USB / Wi-Fi ADB").grid(row=1, sticky="w")
        self.phone = ttk.Combobox(content, state="readonly", values=["自动选择在线手机"])
        self.phone.current(0)
        self.phone.grid(row=2, sticky="ew", pady=(4, 12))
        network = ttk.Frame(content)
        network.grid(row=3, sticky="w", pady=(0, 12))
        self.wifi_button = ttk.Button(network, text="USB 转无线", command=self.wifi_connect)
        self.wifi_button.grid(row=0, column=0)
        ttk.Label(content, text="电脑音频来源").grid(row=4, sticky="w")
        self.capture = ttk.Combobox(content, state="readonly")
        self.capture.grid(row=5, sticky="ew", pady=(4, 16))
        ttk.Label(content, text="手机麦克风接收设备（虚拟音频线播放端）").grid(row=6, sticky="w")
        self.sink = ttk.Combobox(content, state="readonly")
        self.sink.grid(row=7, sticky="ew", pady=(4, 16))
        mode_row = ttk.Frame(content)
        mode_row.columnconfigure(1, weight=1)
        mode_row.grid(row=8, sticky="ew", pady=(0, 16))
        ttk.Label(mode_row, text="播放模式").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.mode = ttk.Combobox(mode_row, state="readonly", values=[
            "自动：麦克风回传时使用通话模式",
            "影音优先：麦克风保持回传",
            "通话优先：始终启用 AEC",
            "跟随 Windows 麦克风：检测所有使用程序",
        ])
        self.mode.current(3)
        self.mode.grid(row=0, column=1, sticky="ew")
        latency_row = ttk.Frame(content)
        latency_row.columnconfigure(1, weight=1)
        latency_row.grid(row=9, sticky="ew", pady=(0, 16))
        ttk.Label(latency_row, text="传输延迟").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.latency = ttk.Combobox(latency_row, state="readonly", values=[
            "低延迟：5 ms 帧",
            "稳定：10 ms 帧",
        ])
        self.latency.current(0)
        self.latency.grid(row=0, column=1, sticky="ew")
        quality_row = ttk.Frame(content)
        quality_row.columnconfigure(1, weight=1)
        quality_row.grid(row=10, sticky="ew", pady=(0, 16))
        ttk.Label(quality_row, text="音频品质").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.quality = ttk.Combobox(quality_row, state="readonly", values=[
            "标准：48 kHz / 16-bit PCM",
            "中品质：96 kHz / 24-bit PCM",
            "无损品质：192 kHz / 24-bit PCM",
        ])
        self.quality.current(0)
        self.quality.grid(row=0, column=1, sticky="ew")
        buttons = ttk.Frame(content)
        buttons.grid(row=11, sticky="w", pady=(0, 16))
        self.start_button = ttk.Button(buttons, text="连接", command=self.start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        self.refresh_button = ttk.Button(buttons, text="刷新设备", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.mic_button = ttk.Button(buttons, text="测试麦克风", command=lambda: self.start(True))
        self.mic_button.pack(side="left", padx=(8, 0))
        self.capability_button = ttk.Button(buttons, text="检测声道", command=self.check_capabilities)
        self.capability_button.pack(side="left", padx=(8, 0))
        self.state = ttk.Label(content, text="就绪")
        self.state.grid(row=12, sticky="ew", pady=(0, 8))
        self.state.bind("<Configure>", lambda event: self.state.configure(wraplength=max(100, event.width)))
        self.log = tk.Text(content, height=8, wrap="word", state="disabled", font=("Consolas", 10))
        self.log.grid(row=13, sticky="nsew")
        self.refresh()
        root.after(200, self.poll)

    def refresh(self):
        try:
            import soundcard as sc
            self.devices = sc.all_speakers()
            names = [speaker.name for speaker in self.devices]
            self.capture["values"] = names
            self.sink["values"] = ["关闭麦克风回传"] + names
            default = sc.default_speaker()
            if self.devices:
                index = next((i for i, item in enumerate(self.devices)
                              if default and item.id == default.id), 0)
                self.capture.current(index)
            self.sink.current(0)
        except Exception as error:
            messagebox.showerror("音频设备", str(error))
        self.refresh_adb()

    def refresh_adb(self, convert_usb=False):
        if self.network_busy or (self.thread and self.thread.is_alive()):
            return
        self.network_busy = True
        self.refresh_button["state"] = "disabled"
        self.wifi_button["state"] = "disabled"
        selected = self.phones[self.phone.current() - 1].serial if self.phone.current() > 0 else None
        selected_device = self.phones[self.phone.current() - 1] if self.phone.current() > 0 else None

        def work():
            devices = []
            try:
                selected_after = selected
                if convert_usb:
                    usb_serial = (selected_device.serial if selected_device
                                  and selected_device.transport == "usb" else None)
                    selected_after = enable_wifi_adb(usb_serial)
                    self.events.put(("log", f"USB 已转为无线 ADB：{selected_after}"))
                devices = list_adb_devices()
                if not devices:
                    self.events.put(("log", "未检测到手机；检查 USB 调试或无线 ADB 连接。"))
            except Exception as error:
                self.events.put(("log", f"ADB: {error}"))
            finally:
                self.events.put(("devices", (devices, selected_after if 'selected_after' in locals() else selected)))

        self.thread = threading.Thread(target=work, name="adb-discovery")
        self.thread.start()

    def wifi_connect(self):
        self.refresh_adb(convert_usb=True)

    def check_capabilities(self):
        if self.network_busy or (self.thread and self.thread.is_alive()):
            return
        if self.capture.current() < 0:
            messagebox.showerror("音频设备", "请选择电脑音频来源。")
            return
        selected = self.phones[self.phone.current() - 1] if self.phone.current() > 0 else None
        if selected is None or selected.state != "device":
            messagebox.showerror("手机设备", "请选择一台在线手机。")
            return
        capture_id = self.devices[self.capture.current()].id
        self.network_busy = True
        self.capability_button["state"] = "disabled"
        self.refresh_button["state"] = "disabled"

        def work():
            try:
                windows = windows_audio_capabilities(capture_id)
                android = query_android_audio_capabilities(selected.serial)
                available = min(windows["channels"], android["max_channels"])
                actual = min(2, available)
                self.events.put(("log",
                    f"Windows: {windows['name']} | {windows['layout']} ({windows['channels']} 声道)\n"
                    f"Android: {android['device']} | 当前 {android['layout']} | 最大 {android['max_layout']}\n"
                    f"Android PCM: 当前 {android['rate'] // 1000} kHz / {android['bits']}-bit | "
                    f"最大 {android['max_rate'] // 1000} kHz / {android['max_bits']}-bit\n"
                    f"共同能力: {channel_layout(available)} | 当前协议实际传输: {channel_layout(actual)}"))
            except Exception as error:
                self.events.put(("log", f"声道检测失败: {error}"))
            finally:
                self.events.put(("capability_done", "声道检测完成"))

        self.thread = threading.Thread(target=work, name="audio-capabilities")
        self.thread.start()

    def start(self, test_mic=False):
        if self.network_busy:
            return
        if self.thread and self.thread.is_alive():
            return
        if not test_mic and self.capture.current() < 0:
            messagebox.showerror("音频设备", "请选择电脑音频来源。")
            return
        capture = self.devices[self.capture.current()].id if self.capture.current() >= 0 else None
        sink = self.devices[self.sink.current() - 1].id if self.sink.current() > 0 else None
        selected = self.phones[self.phone.current() - 1] if self.phone.current() > 0 else None
        if selected and selected.state != "device":
            messagebox.showerror("手机设备", f"{selected.serial}: {selected.state}；请完成调试授权或重新连接。")
            return
        playback_mode = ("auto", "media", "communication", "follow")[self.mode.current()]
        latency_profile = ("low", "stable")[self.latency.current()]
        quality_profile = ("standard", "high", "lossless")[self.quality.current()]
        self.bridge = Bridge(capture, sink, serial=selected.serial if selected else None,
                             log=lambda text: self.events.put(("log", text)),
                             playback_mode=playback_mode, latency_profile=latency_profile,
                             quality_profile=quality_profile)
        self.start_button["state"] = "disabled"
        self.stop_button["state"] = "normal"
        self.refresh_button["state"] = "disabled"
        self.capture["state"] = "disabled"
        self.sink["state"] = "disabled"
        self.phone["state"] = "disabled"
        self.mode["state"] = "disabled"
        self.latency["state"] = "disabled"
        self.quality["state"] = "disabled"
        self.wifi_button["state"] = "disabled"
        self.mic_button["state"] = "disabled"
        self.capability_button["state"] = "disabled"
        self.state["text"] = "正在连接"

        def work():
            try:
                if test_mic:
                    self.bridge.test_microphone()
                else:
                    self.bridge.run()
            except Exception as error:
                if not self.bridge.stop_event.is_set():
                    self.events.put(("log", f"Error: {error}"))
            finally:
                self.events.put(("done", "已停止"))

        self.thread = threading.Thread(target=work, name="usb-audio-bridge")
        self.thread.start()

    def stop(self):
        if self.bridge:
            self.bridge.stop()
        self.stop_button["state"] = "disabled"
        self.state["text"] = "正在停止"

    def close(self):
        self.closing = True
        self.stop()

    def poll(self):
        while True:
            try:
                kind, text = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "devices":
                self.phones, selected = text
                self.phone["values"] = ["自动选择在线手机"] + [
                    f"{item.model} | {item.transport} | {item.serial} | {item.state}"
                    for item in self.phones]
                index = next((i + 1 for i, item in enumerate(self.phones) if item.serial == selected), 0)
                if index == 0 and len(self.phones) == 1:
                    index = 1
                self.phone.current(index)
                self.network_busy = False
                self.refresh_button["state"] = "normal"
                self.wifi_button["state"] = "normal"
                self.capability_button["state"] = "normal"
            elif kind == "capability_done":
                self.network_busy = False
                self.capability_button["state"] = "normal"
                self.refresh_button["state"] = "normal"
                self.state["text"] = text
            elif kind == "done":
                self.start_button["state"] = "normal"
                self.stop_button["state"] = "disabled"
                self.refresh_button["state"] = "normal"
                self.capture["state"] = "readonly"
                self.sink["state"] = "readonly"
                self.phone["state"] = "readonly"
                self.mode["state"] = "readonly"
                self.latency["state"] = "readonly"
                self.quality["state"] = "readonly"
                self.wifi_button["state"] = "normal"
                self.mic_button["state"] = "normal"
                self.capability_button["state"] = "normal"
                self.state["text"] = text
            else:
                self.state["text"] = text
                self.log["state"] = "normal"
                self.log.insert("end", text + "\n")
                if int(self.log.index("end-1c").split(".")[0]) > 200:
                    self.log.delete("1.0", "2.0")
                self.log.see("end")
                self.log["state"] = "disabled"
        if self.closing and (not self.thread or not self.thread.is_alive()):
            self.root.destroy()
            return
        self.root.after(200, self.poll)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        try:
            from check_environment import check_audio_dependencies
            from bridge import resolve_adb_executable
            check_audio_dependencies()
            resolve_adb_executable()
        except Exception:
            Path(sys.executable).with_name("self-test-error.log").write_text(
                traceback.format_exc(), encoding="utf-8")
            raise SystemExit(1)
        raise SystemExit(0)
    window = tk.Tk()
    App(window)
    window.mainloop()
