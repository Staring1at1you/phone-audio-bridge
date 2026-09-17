package dev.usbaudio;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioDeviceInfo;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.AudioManager;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.NoiseSuppressor;
import android.net.wifi.WifiManager;
import android.os.IBinder;
import android.util.Log;
import java.io.Closeable;
import java.io.DataInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.SocketTimeoutException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;

public final class AudioService extends Service {
    public static volatile boolean running;
    public static volatile boolean playbackConnected;
    public static volatile boolean micConnected;
    public static volatile boolean micMuted;
    public static volatile boolean echoCancellation = true;
    public static volatile float volume = 0.7f;
    public static volatile String status = "已停止";
    public static volatile String aecStatus = "AEC 待连接";
    public static volatile String playbackModeStatus = "模式待连接";
    public static volatile String qualityStatus = "品质待连接";
    private static final String TAG = "UsbAudio";
    private final java.util.Map<AudioTrack, PlaybackEffects> playbackEffects = new ConcurrentHashMap<>();
    private static final int RATE = 48000;
    private static final int FRAMES = 480;
    private static final int PLAY_PORT = 27183;
    private static final int MIC_PORT = 27184;
    private static final int UDP_PLAY_PORT = 27185;
    private static final int CONTROL_PORT = 27187;
    private static final int UDP_FRAMES = 240;
    private static final int UDP_HEADER = 34;
    private final Set<Closeable> connections = ConcurrentHashMap.newKeySet();
    private volatile boolean active;
    private ServerSocket playbackServer;
    private ServerSocket micServer;
    private ServerSocket controlServer;
    private DatagramSocket udpPlaybackSocket;
    private AudioManager audioManager;
    private int previousAudioMode = AudioManager.MODE_NORMAL;
    private boolean previousSpeakerphone;
    private int communicationSession;
    private volatile boolean communicationMode;
    private static volatile AcousticEchoCanceler activeEchoCanceler;
    private static volatile NoiseSuppressor activeNoiseSuppressor;
    private volatile UdpConfig udpConfig;
    private volatile int udpGeneration;
    private WifiManager.WifiLock wifiLock;
    private volatile Thread udpMicThread;

    private static final class UdpConfig {
        final long token;
        final InetAddress computer;
        final int micPort;
        final boolean communication;
        final boolean microphone;
        final boolean stable;
        final int rate;
        final int bits;
        final int generation;

        UdpConfig(long token, InetAddress computer, int micPort, int flags, int rate,
                  int bits, int generation) {
            this.token = token;
            this.computer = computer;
            this.micPort = micPort;
            this.communication = (flags & 1) != 0;
            this.microphone = (flags & 2) != 0;
            this.stable = (flags & 4) != 0;
            this.rate = rate;
            this.bits = bits;
            this.generation = generation;
        }
    }

    private static final class StreamConfig {
        final boolean communication;
        final int frames;
        final int rate;
        final int bits;

        StreamConfig(boolean communication, int frames, int rate, int bits) {
            this.communication = communication;
            this.frames = frames;
            this.rate = rate;
            this.bits = bits;
        }
    }

    private static final class FragmentSet {
        final byte[][] parts;
        int received;

        FragmentSet(int count) { parts = new byte[count][]; }

        byte[] add(int index, byte[] data) {
            if (parts[index] == null) {
                parts[index] = data;
                received++;
            }
            if (received != parts.length) return null;
            int length = 0;
            for (byte[] part : parts) length += part.length;
            byte[] complete = new byte[length];
            int offset = 0;
            for (byte[] part : parts) {
                System.arraycopy(part, 0, complete, offset, part.length);
                offset += part.length;
            }
            return complete;
        }
    }

    private static final class OutputCapability {
        final int currentChannels;
        final int maximumChannels;
        final int deviceType;
        final int maxBits;
        final int maxRate;

        OutputCapability(int currentChannels, int maximumChannels, int deviceType,
                         int maxBits, int maxRate) {
            this.currentChannels = currentChannels;
            this.maximumChannels = maximumChannels;
            this.deviceType = deviceType;
            this.maxBits = maxBits;
            this.maxRate = maxRate;
        }
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (active) return START_NOT_STICKY;
        PlaybackEffects.load(this);
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel(
                "audio", "Phone Audio Bridge", NotificationManager.IMPORTANCE_LOW));
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification notification = new Notification.Builder(this, "audio")
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setContentTitle("Phone Audio Bridge 正在运行")
                .setContentText("电脑音频 / 手机麦克风")
                .setContentIntent(open).setOngoing(true).build();
        startForeground(1, notification);
        try {
            audioManager = getSystemService(AudioManager.class);
            previousAudioMode = audioManager.getMode();
            previousSpeakerphone = audioManager.isSpeakerphoneOn();
            communicationSession = audioManager.generateAudioSessionId();
            if (communicationSession == AudioManager.ERROR) {
                throw new IOException("通信音频会话创建失败");
            }
            playbackServer = listen(PLAY_PORT);
            micServer = listen(MIC_PORT);
            controlServer = listen(CONTROL_PORT);
            udpPlaybackSocket = new DatagramSocket(new InetSocketAddress(
                    InetAddress.getByName("0.0.0.0"), UDP_PLAY_PORT));
            udpPlaybackSocket.setSoTimeout(500);
            WifiManager wifi = getSystemService(WifiManager.class);
            wifiLock = wifi.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "UsbAudio:udp");
            wifiLock.setReferenceCounted(false);
            wifiLock.acquire();
            active = true;
            running = true;
            status = "等待电脑连接";
            new Thread(() -> acceptLoop(playbackServer, false), "usb-playback").start();
            new Thread(() -> acceptLoop(micServer, true), "usb-mic").start();
            new Thread(this::controlLoop, "udp-control").start();
            new Thread(this::udpPlaybackLoop, "udp-playback").start();
        } catch (IOException error) {
            status = "端口启动失败：" + error.getMessage();
            Log.e(TAG, status, error);
            close(playbackServer);
            close(micServer);
            close(controlServer);
            close(udpPlaybackSocket);
            stopSelf();
        }
        return START_NOT_STICKY;
    }

    private static boolean hasPrivateAudioOutput(AudioManager manager) {
        for (AudioDeviceInfo device : manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
            switch (device.getType()) {
                case AudioDeviceInfo.TYPE_WIRED_HEADPHONES:
                case AudioDeviceInfo.TYPE_WIRED_HEADSET:
                case AudioDeviceInfo.TYPE_USB_HEADSET:
                case AudioDeviceInfo.TYPE_BLUETOOTH_A2DP:
                case AudioDeviceInfo.TYPE_BLUETOOTH_SCO:
                    return true;
                default:
                    break;
            }
        }
        return false;
    }

    private static boolean isPrivateOutputType(int type) {
        return type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES
                || type == AudioDeviceInfo.TYPE_WIRED_HEADSET
                || type == AudioDeviceInfo.TYPE_USB_DEVICE
                || type == AudioDeviceInfo.TYPE_USB_HEADSET
                || type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
                || type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO;
    }

    private OutputCapability getOutputCapability() {
        AudioDeviceInfo selected = null;
        for (AudioDeviceInfo device : audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
            if (!device.isSink()) continue;
            if (isPrivateOutputType(device.getType())) {
                selected = device;
                break;
            }
            if (selected == null && device.getType() == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER) {
                selected = device;
            }
        }
        if (selected == null) return new OutputCapability(2, 2, 0, 16, 48000);
        int maximum = 0;
        for (int count : selected.getChannelCounts()) maximum = Math.max(maximum, count);
        for (int mask : selected.getChannelMasks()) {
            maximum = Math.max(maximum, Integer.bitCount(mask & 0x3fffffff));
        }
        if (maximum <= 0) maximum = selected.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ? 1 : 2;
        int current = Math.min(2, maximum);
        int maxBits = android.os.Build.VERSION.SDK_INT >= 31 ? 24 : 16;
        int maxRate = 48000;
        if (android.os.Build.VERSION.SDK_INT >= 31) {
            for (int candidate : new int[]{96000, 192000}) {
                if (AudioTrack.getMinBufferSize(candidate, AudioFormat.CHANNEL_OUT_STEREO,
                        AudioFormat.ENCODING_PCM_24BIT_PACKED) > 0) maxRate = candidate;
            }
        }
        return new OutputCapability(current, maximum, selected.getType(), maxBits, maxRate);
    }

    private void writeCapabilityAck(OutputStream output, int generation, int actualBits,
                                    int actualRate) throws IOException {
        OutputCapability capability = getOutputCapability();
        output.write(ByteBuffer.allocate(28).order(ByteOrder.BIG_ENDIAN)
                .putInt(0x55414b41).putInt(generation)
                .putShort((short) capability.currentChannels)
                .putShort((short) capability.maximumChannels)
                .putInt(capability.deviceType)
                .putShort((short) actualBits).putShort((short) capability.maxBits)
                .putInt(actualRate).putInt(capability.maxRate).array());
    }

    private ServerSocket listen(int port) throws IOException {
        ServerSocket server = new ServerSocket();
        try {
            server.setReuseAddress(true);
            server.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port));
            return server;
        } catch (IOException error) {
            close(server);
            throw error;
        }
    }

    private void controlLoop() {
        while (active) {
            try (Socket socket = controlServer.accept()) {
                socket.setSoTimeout(5000);
                DataInputStream input = new DataInputStream(socket.getInputStream());
                if (input.readInt() != 0x55414331) throw new IOException("UDP 控制协议错误");
                long token = input.readLong();
                int ipv4 = input.readInt();
                int sampleRate = input.readInt();
                int micPort = input.readUnsignedShort();
                int frames = input.readUnsignedShort();
                int requestedBits = input.readUnsignedShort();
                int flags = input.readInt();
                if ((flags & 16) != 0) {
                    OutputCapability capability = getOutputCapability();
                    writeCapabilityAck(socket.getOutputStream(), udpGeneration,
                            Math.min(16, capability.maxBits), 48000);
                    continue;
                }
                if ((sampleRate != 48000 && sampleRate != 96000 && sampleRate != 192000)
                        || frames != sampleRate / 200 || micPort <= 0
                        || (requestedBits != 16 && requestedBits != 24)) {
                    throw new IOException("UDP 参数错误");
                }
                byte[] address = new byte[]{
                        (byte) (ipv4 >>> 24), (byte) (ipv4 >>> 16),
                        (byte) (ipv4 >>> 8), (byte) ipv4};
                int generation = ++udpGeneration;
                OutputCapability capability = getOutputCapability();
                boolean communication = (flags & 1) != 0;
                int actualRate = communication ? 48000 : Math.min(sampleRate, capability.maxRate);
                int actualBits = communication ? 16 : Math.min(requestedBits, capability.maxBits);
                UdpConfig config = (flags & 8) != 0 ? null : new UdpConfig(
                        token, InetAddress.getByAddress(address), micPort, flags,
                        actualRate, actualBits, generation);
                udpConfig = config;
                Thread previousMic = udpMicThread;
                if (previousMic != null && previousMic.isAlive()) {
                    try { previousMic.join(1000); }
                    catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
                }
                if (config != null && config.microphone) {
                    Thread microphone = new Thread(
                            () -> udpRecordLoop(config), "udp-mic-" + generation);
                    udpMicThread = microphone;
                    microphone.start();
                } else if (config == null) {
                    configurePlaybackMode(false);
                    qualityStatus = "品质待连接";
                }
                writeCapabilityAck(socket.getOutputStream(), generation, actualBits, actualRate);
                if (config == null) {
                    Log.i(TAG, "UDP 会话已停止");
                } else {
                    Log.i(TAG, "UDP 会话=" + generation + "，电脑=" + config.computer
                            + "，模式=" + (config.communication ? "通话" : "影音"));
                }
            } catch (IOException | RuntimeException error) {
                if (active) Log.w(TAG, "UDP 控制异常：" + error.getMessage(), error);
            }
        }
    }

    private void udpPlaybackLoop() {
        byte[] datagram = new byte[1500];
        AudioTrack track = null;
        UdpConfig current = null;
        TreeMap<Integer, byte[]> jitter = new TreeMap<>();
        TreeMap<Integer, FragmentSet> fragments = new TreeMap<>();
        int expected = -1;
        byte[] hardwareBlock = null;
        int hardwareOffset = 0;
        while (active) {
            try {
                DatagramPacket packet = new DatagramPacket(datagram, datagram.length);
                udpPlaybackSocket.receive(packet);
                UdpConfig config = udpConfig;
                if (config == null || !packet.getAddress().equals(config.computer)
                        || packet.getLength() <= UDP_HEADER || packet.getLength() > 1500) continue;
                ByteBuffer header = ByteBuffer.wrap(packet.getData(), 0, packet.getLength())
                        .order(ByteOrder.BIG_ENDIAN);
                if (header.getInt() != 0x55415531 || header.getLong() != config.token) continue;
                int sequence = header.getInt();
                header.getInt();
                int rate = header.getInt();
                int channels = header.getShort() & 0xffff;
                int bits = header.getShort() & 0xffff;
                int frames = header.getShort() & 0xffff;
                int fragment = header.getShort() & 0xffff;
                int fragmentCount = header.getShort() & 0xffff;
                if (rate != config.rate || channels != 2 || bits != config.bits
                        || frames != rate / 200 || fragment >= fragmentCount
                        || fragmentCount < 1) continue;
                if (current == null || current.generation != config.generation) {
                    if (track != null) releasePlayback(track);
                    configurePlaybackMode(config.communication);
                    track = createTrack(frames, rate, bits);
                    track.play();
                    current = config;
                    jitter.clear();
                    fragments.clear();
                    expected = -1;
                    hardwareOffset = 0;
                    hardwareBlock = new byte[frames * 2 * (bits / 8) * 2];
                    playbackConnected = true;
                    status = "Wi-Fi UDP 已连接";
                    qualityStatus = (rate / 1000) + " kHz / " + bits + "-bit PCM";
                }
                if (expected >= 0 && sequence < expected) continue;
                byte[] fragmentData = Arrays.copyOfRange(
                        packet.getData(), UDP_HEADER, packet.getLength());
                FragmentSet set = fragments.get(sequence);
                if (set == null) {
                    set = new FragmentSet(fragmentCount);
                    fragments.put(sequence, set);
                }
                if (set.parts.length != fragmentCount) {
                    fragments.remove(sequence);
                    continue;
                }
                byte[] pcm = set.add(fragment, fragmentData);
                if (pcm == null) continue;
                fragments.remove(sequence);
                while (fragments.size() > 8) fragments.pollFirstEntry();
                if (pcm.length != frames * 2 * (bits / 8)) continue;
                jitter.putIfAbsent(sequence, pcm);
                int target = config.stable ? 4 : 2;
                if (expected < 0 && jitter.size() >= target) expected = jitter.firstKey();
                if (expected >= 0 && !jitter.containsKey(expected) && jitter.size() >= target + 2) {
                    expected = jitter.firstKey();
                }
                while (expected >= 0 && jitter.containsKey(expected)) {
                    byte[] ordered = jitter.remove(expected++);
                    System.arraycopy(ordered, 0, hardwareBlock, hardwareOffset, ordered.length);
                    hardwareOffset += ordered.length;
                    if (hardwareOffset == hardwareBlock.length) {
                        writeTrack(track, hardwareBlock);
                        hardwareOffset = 0;
                    }
                }
            } catch (SocketTimeoutException ignored) {
                UdpConfig config = udpConfig;
                if (current != null && (config == null || current.generation != config.generation)) {
                    if (track != null) releasePlayback(track);
                    track = null;
                    current = null;
                    playbackConnected = false;
                }
            } catch (IOException | RuntimeException error) {
                if (active) Log.w(TAG, "UDP 播放异常：" + error.getMessage(), error);
            }
        }
        if (track != null) releasePlayback(track);
    }

    private AudioTrack createTrack(int frames, int rate, int bits) throws IOException {
        int encoding = bits == 24 ? AudioFormat.ENCODING_PCM_24BIT_PACKED
                : AudioFormat.ENCODING_PCM_16BIT;
        int minimum = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_STEREO,
                encoding);
        if (minimum <= 0) throw new IOException("播放格式不支持");
        AudioTrack track = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(communicationMode ? AudioAttributes.USAGE_VOICE_COMMUNICATION
                                : AudioAttributes.USAGE_MEDIA)
                        .setContentType(communicationMode ? AudioAttributes.CONTENT_TYPE_SPEECH
                                : AudioAttributes.CONTENT_TYPE_MUSIC).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(rate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                        .setEncoding(encoding).build())
                .setBufferSizeInBytes(Math.max(minimum, frames * 2 * (bits / 8) * 2))
                .setSessionId(communicationSession)
                .setPerformanceMode(communicationMode ? AudioTrack.PERFORMANCE_MODE_LOW_LATENCY
                        : AudioTrack.PERFORMANCE_MODE_NONE)
                .setTransferMode(AudioTrack.MODE_STREAM).build();
        if (track.getState() != AudioTrack.STATE_INITIALIZED) {
            track.release();
            throw new IOException("播放器初始化失败");
        }
        playbackEffects.put(track, new PlaybackEffects(this, track.getAudioSessionId(), communicationMode));
        return track;
    }

    private void writeTrack(AudioTrack track, byte[] pcm) throws IOException {
        PlaybackEffects effects = playbackEffects.get(track);
        track.setVolume(volume * (effects == null ? 1f : effects.gain()));
        int offset = 0;
        while (active && offset < pcm.length) {
            int written = track.write(pcm, offset, pcm.length - offset, AudioTrack.WRITE_BLOCKING);
            if (written <= 0) throw new IOException("播放写入失败：" + written);
            offset += written;
        }
    }

    private void releasePlayback(AudioTrack track) {
        PlaybackEffects effects = playbackEffects.remove(track);
        if (effects != null) effects.close();
        track.release();
    }

    private void udpRecordLoop(UdpConfig config) {
        AudioRecord recorder = null;
        try {
            if (!active || udpGeneration != config.generation) return;
            configurePlaybackMode(config.communication);
            int minimum = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT);
            recorder = new AudioRecord.Builder()
                    .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE)
                            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                    .setBufferSizeInBytes(Math.max(minimum, UDP_FRAMES * 2 * 4)).build();
            if (recorder.getState() != AudioRecord.STATE_INITIALIZED) throw new IOException("麦克风初始化失败");
            configureVoiceEffects(recorder.getAudioSessionId(), communicationMode);
            recorder.startRecording();
            int sequence = 0;
            byte[] pcm = new byte[UDP_FRAMES * 2 * 2];
            while (active && udpGeneration == config.generation) {
                int offset = 0;
                while (offset < pcm.length) {
                    int read = recorder.read(pcm, offset, pcm.length - offset, AudioRecord.READ_BLOCKING);
                    if (read <= 0) throw new IOException("麦克风读取失败：" + read);
                    offset += read;
                }
                if (micMuted) Arrays.fill(pcm, (byte) 0);
                for (int part = 0; part < 2; part++) {
                    ByteBuffer output = ByteBuffer.allocate(UDP_HEADER + UDP_FRAMES * 2)
                            .order(ByteOrder.BIG_ENDIAN);
                    output.putInt(0x55415531).putLong(config.token).putInt(sequence++)
                            .putInt((int) (System.nanoTime() / 1000)).putInt(RATE)
                            .putShort((short) 1).putShort((short) 16)
                            .putShort((short) UDP_FRAMES)
                            .putShort((short) 0).putShort((short) 1)
                            .put(pcm, part * UDP_FRAMES * 2, UDP_FRAMES * 2);
                    byte[] data = output.array();
                    udpPlaybackSocket.send(new DatagramPacket(
                            data, data.length, config.computer, config.micPort));
                }
                micConnected = true;
            }
        } catch (IOException | RuntimeException error) {
            if (active && udpGeneration == config.generation) {
                Log.w(TAG, "UDP 麦克风异常：" + error.getMessage(), error);
            }
        } finally {
            if (recorder != null) recorder.release();
            if (udpGeneration == config.generation) micConnected = false;
            releaseVoiceEffects();
            if (Thread.currentThread() == udpMicThread) udpMicThread = null;
        }
    }

    private void acceptLoop(ServerSocket server, boolean microphone) {
        while (active) {
            Socket socket = null;
            try {
                socket = server.accept();
                connections.add(socket);
                if (!active) break;
                socket.setTcpNoDelay(true);
                socket.setSoTimeout(5000);
                DataInputStream input = new DataInputStream(socket.getInputStream());
                StreamConfig stream = checkHeader(input, microphone ? 1 : 2);
                if (microphone) record(socket, stream.communication, stream.frames);
                else play(input, stream.communication, stream.frames, stream.rate, stream.bits);
            } catch (IOException | RuntimeException error) {
                if (active) {
                    status = (microphone ? "麦克风" : "播放") + "断开：" + error.getMessage();
                    Log.w(TAG, status, error);
                }
            } finally {
                if (microphone) micConnected = false; else playbackConnected = false;
                if (socket != null) {
                    connections.remove(socket);
                    close(socket);
                }
            }
        }
    }

    private StreamConfig checkHeader(DataInputStream input, int channels) throws IOException {
        byte[] header = new byte[16];
        input.readFully(header);
        ByteBuffer buffer = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN);
        int rate = buffer.getInt(4);
        int bits = buffer.getShort(10) & 0xffff;
        int encodedFrames = buffer.getInt(12);
        int frames = encodedFrames & 0x7fffffff;
        if (buffer.getInt() != 0x55414231 || buffer.getInt() != rate
                || buffer.getShort() != channels || buffer.getShort() != bits
                || (rate != 48000 && rate != 96000 && rate != 192000)
                || (bits != 16 && bits != 24)
                || (frames != rate / 200 && frames != rate / 100)) {
            throw new IOException("音频协议不匹配");
        }
        return new StreamConfig((encodedFrames & 0x80000000) != 0,
                frames, rate, bits);
    }

    private void play(DataInputStream input, boolean requestedCommunication, int frames,
                      int rate, int bits) throws IOException {
        configurePlaybackMode(requestedCommunication);
        int ioFrames = Math.max(frames, rate / 100);
        int frameBytes = ioFrames * 2 * (bits / 8);
        int encoding = bits == 24 ? AudioFormat.ENCODING_PCM_24BIT_PACKED
                : AudioFormat.ENCODING_PCM_16BIT;
        int minimum = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_STEREO,
                encoding);
        if (minimum <= 0) throw new IOException("播放格式不支持");
        AudioTrack track = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(communicationMode ? AudioAttributes.USAGE_VOICE_COMMUNICATION
                                : AudioAttributes.USAGE_MEDIA)
                        .setContentType(communicationMode ? AudioAttributes.CONTENT_TYPE_SPEECH
                                : AudioAttributes.CONTENT_TYPE_MUSIC).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(rate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                        .setEncoding(encoding).build())
                .setBufferSizeInBytes(Math.max(minimum, frames * 2 * (bits / 8) * 2))
                .setSessionId(communicationSession)
                .setPerformanceMode(communicationMode && frames == 240 ? AudioTrack.PERFORMANCE_MODE_LOW_LATENCY
                        : AudioTrack.PERFORMANCE_MODE_NONE)
                .setTransferMode(AudioTrack.MODE_STREAM).build();
        try {
            if (track.getState() != AudioTrack.STATE_INITIALIZED) throw new IOException("播放器初始化失败");
            playbackEffects.put(track, new PlaybackEffects(this, track.getAudioSessionId(), communicationMode));
            playbackConnected = true;
            status = "USB 音频已连接";
            qualityStatus = (rate / 1000) + " kHz / " + bits + "-bit PCM";
            track.play();
            byte[] pcm = new byte[frameBytes];
            while (active) {
                input.readFully(pcm);
                writeTrack(track, pcm);
            }
        } finally {
            releasePlayback(track);
        }
    }

    private synchronized void configurePlaybackMode(boolean requestedCommunication) {
        boolean privateOutput = hasPrivateAudioOutput(audioManager);
        communicationMode = requestedCommunication && !privateOutput;
        if (communicationMode) {
            audioManager.setMode(AudioManager.MODE_IN_COMMUNICATION);
            audioManager.setSpeakerphoneOn(true);
            playbackModeStatus = "通话模式 · AEC";
        } else {
            audioManager.setSpeakerphoneOn(false);
            audioManager.setMode(AudioManager.MODE_NORMAL);
            playbackModeStatus = privateOutput ? "影音模式 · 耳机" : "影音模式";
        }
        Log.i(TAG, "播放模式=" + playbackModeStatus + "，AudioMode=" + audioManager.getMode());
    }

    private void record(Socket socket, boolean requestedCommunication, int frames) throws IOException {
        configurePlaybackMode(requestedCommunication);
        int minimum = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        if (minimum <= 0) throw new IOException("麦克风格式不支持");
        AudioRecord recorder = new AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE)
                        .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setBufferSizeInBytes(Math.max(minimum, frames * 2 * 2)).build();
        try {
            if (recorder.getState() != AudioRecord.STATE_INITIALIZED) throw new IOException("麦克风初始化失败");
            configureVoiceEffects(recorder.getAudioSessionId(), communicationMode);
            recorder.startRecording();
            if (recorder.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
                throw new IOException("麦克风启动失败");
            }
            micConnected = true;
            status = "USB 音频已连接";
            OutputStream output = socket.getOutputStream();
            int ioFrames = Math.max(frames, 480);
            byte[] pcm = new byte[ioFrames * 2];
            while (active) {
                int offset = 0;
                while (active && offset < pcm.length) {
                    int read = recorder.read(pcm, offset, pcm.length - offset, AudioRecord.READ_BLOCKING);
                    if (read <= 0) throw new IOException("麦克风读取失败：" + read);
                    offset += read;
                }
                if (!active) break;
                if (micMuted) Arrays.fill(pcm, (byte) 0);
                output.write(pcm);
            }
        } finally {
            releaseVoiceEffects();
            recorder.release();
        }
    }

    public static synchronized void setEchoCancellation(boolean enabled) {
        echoCancellation = enabled;
        if (activeEchoCanceler != null) {
            int result = activeEchoCanceler.setEnabled(enabled);
            aecStatus = result == android.media.audiofx.AudioEffect.SUCCESS
                    ? (enabled ? "AEC 已启用" : "AEC 已关闭") : "AEC 切换失败";
        }
        if (activeNoiseSuppressor != null) activeNoiseSuppressor.setEnabled(enabled);
    }

    private static synchronized void configureVoiceEffects(int sessionId, boolean enableAec) {
        releaseVoiceEffects();
        if (!enableAec) {
            aecStatus = "影音模式 · AEC 未启用";
            return;
        }
        if (!AcousticEchoCanceler.isAvailable()) {
            aecStatus = "设备不支持 AEC";
            Log.w(TAG, aecStatus);
            return;
        }
        try {
            activeEchoCanceler = AcousticEchoCanceler.create(sessionId);
            if (activeEchoCanceler == null) {
                aecStatus = "AEC 创建失败";
            } else {
                int result = activeEchoCanceler.setEnabled(echoCancellation);
                aecStatus = result == android.media.audiofx.AudioEffect.SUCCESS
                        ? (echoCancellation ? "AEC 已启用" : "AEC 已关闭") : "AEC 启用失败";
                Log.i(TAG, aecStatus + "，会话=" + sessionId
                        + "，控制权=" + activeEchoCanceler.hasControl());
            }
            if (NoiseSuppressor.isAvailable()) {
                activeNoiseSuppressor = NoiseSuppressor.create(sessionId);
                if (activeNoiseSuppressor != null) activeNoiseSuppressor.setEnabled(echoCancellation);
            }
        } catch (RuntimeException error) {
            aecStatus = "AEC 异常：" + error.getMessage();
            Log.w(TAG, aecStatus, error);
            releaseVoiceEffects();
        }
    }

    private static synchronized void releaseVoiceEffects() {
        if (activeEchoCanceler != null) {
            activeEchoCanceler.release();
            activeEchoCanceler = null;
        }
        if (activeNoiseSuppressor != null) {
            activeNoiseSuppressor.release();
            activeNoiseSuppressor = null;
        }
    }

    @Override public void onDestroy() {
        active = false;
        running = false;
        playbackConnected = false;
        micConnected = false;
        status = "已停止";
        aecStatus = "AEC 待连接";
        playbackModeStatus = "模式待连接";
        qualityStatus = "品质待连接";
        close(playbackServer);
        close(micServer);
        close(controlServer);
        close(udpPlaybackSocket);
        udpConfig = null;
        udpGeneration++;
        for (Closeable connection : connections) close(connection);
        connections.clear();
        releaseVoiceEffects();
        if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
        if (audioManager != null) {
            audioManager.setSpeakerphoneOn(previousSpeakerphone);
            audioManager.setMode(previousAudioMode);
        }
        stopForeground(STOP_FOREGROUND_REMOVE);
        super.onDestroy();
    }

    private static void close(Closeable object) {
        if (object != null) {
            try { object.close(); } catch (IOException ignored) { }
        }
    }
}
