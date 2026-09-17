package dev.usbaudio;

import android.content.Context;
import android.content.Intent;
import android.media.audiofx.AudioEffect;
import android.media.audiofx.Equalizer;
import android.util.Log;
import java.util.LinkedHashSet;
import java.util.Set;

/** Public audio-effect APIs only; OEM Dolby remains controlled by the system. */
final class PlaybackEffects implements AutoCloseable {
    static final String[] LABELS = {"系统音效 / Dolby 兼容", "音乐", "电影", "语音清晰", "夜间", "关闭应用音效"};
    static volatile int profile;
    private static final Set<PlaybackEffects> LIVE = new LinkedHashSet<>();
    private final Context context;
    private final int session;
    private final boolean communication;
    private Equalizer equalizer;
    private String status = "等待播放";

    static synchronized void select(Context context, int value) {
        if (value < 0 || value >= LABELS.length) return;
        profile = value;
        context.getSharedPreferences("audio", Context.MODE_PRIVATE).edit().putInt("effects", value).apply();
        for (PlaybackEffects effects : LIVE) effects.apply();
    }

    static void load(Context context) {
        profile = context.getSharedPreferences("audio", Context.MODE_PRIVATE).getInt("effects", 0);
        if (profile < 0 || profile >= LABELS.length) profile = 0;
    }

    static String availableVendors() {
        Set<String> names = new LinkedHashSet<>();
        try {
            AudioEffect.Descriptor[] effects = AudioEffect.queryEffects();
            if (effects != null) for (AudioEffect.Descriptor effect : effects) {
                String text = effect.name + " / " + effect.implementor;
                String lower = text.toLowerCase(java.util.Locale.ROOT);
                if (lower.contains("dolby") || lower.contains("dap") || lower.contains("misound")
                        || lower.contains("dts")) names.add(text);
            }
        } catch (RuntimeException error) {
            return "系统音效查询失败：" + error.getMessage();
        }
        return names.isEmpty() ? "未发现 Dolby / DAP / MiSound / DTS 实现"
                : "发现系统实现（不代表已启用）：\n" + String.join("\n", names);
    }

    static synchronized String currentStatus() {
        if (LIVE.isEmpty()) return LABELS[profile] + " · 等待播放";
        return LIVE.iterator().next().status;
    }

    static synchronized int currentSession() {
        return LIVE.isEmpty() ? 0 : LIVE.iterator().next().session;
    }

    PlaybackEffects(Context context, int session, boolean communication) {
        this.context = context;
        this.session = session;
        this.communication = communication;
        synchronized (PlaybackEffects.class) {
            LIVE.add(this);
            if (!communication) announce(AudioEffect.ACTION_OPEN_AUDIO_EFFECT_CONTROL_SESSION);
            apply();
        }
    }

    float gain() {
        return !communication && profile == 4 ? 0.5f : 1f;
    }

    private void announce(String action) {
        Intent intent = new Intent(action)
                .putExtra(AudioEffect.EXTRA_AUDIO_SESSION, session)
                .putExtra(AudioEffect.EXTRA_PACKAGE_NAME, context.getPackageName())
                .putExtra(AudioEffect.EXTRA_CONTENT_TYPE, AudioEffect.CONTENT_TYPE_MUSIC);
        context.sendBroadcast(intent);
    }

    private void apply() {
        if (equalizer != null) {
            equalizer.release();
            equalizer = null;
        }
        if (communication) {
            status = "通话模式 · 应用 EQ 暂停，AEC/NS 生效取决于设备";
            return;
        }
        if (profile == 0 || profile == 5) {
            status = LABELS[profile] + " · Dolby 开关由手机系统控制";
            Log.i("UsbAudio", status);
            return;
        }
        try {
            equalizer = new Equalizer(0, session);
            if (!equalizer.hasControl()) throw new IllegalStateException("音效控制权由系统占用");
            short[] range = equalizer.getBandLevelRange();
            for (short band = 0; band < equalizer.getNumberOfBands(); band++) {
                int hz = equalizer.getCenterFreq(band) / 1000;
                // Attenuation-only curves preserve digital headroom.
                int level;
                if (profile == 1) level = hz >= 300 && hz <= 3000 ? -150 : 0;
                else if (profile == 2) level = hz >= 300 && hz < 2000 ? -250 : 0;
                else if (profile == 3) level = hz < 300 ? -600 : (hz > 5000 ? -250 : 0);
                else level = hz < 300 ? -500 : (hz > 5000 ? -300 : 0);
                equalizer.setBandLevel(band, (short) Math.max(range[0], Math.min(range[1], level)));
            }
            int result = equalizer.setEnabled(true);
            status = result == AudioEffect.SUCCESS && equalizer.getEnabled()
                    ? LABELS[profile] + " · 标准 EQ 已启用" : LABELS[profile] + " · EQ 启用失败";
            Log.i("UsbAudio", status);
        } catch (RuntimeException error) {
            if (equalizer != null) { equalizer.release(); equalizer = null; }
            status = "应用 EQ 未启用：" + error.getMessage();
            Log.w("UsbAudio", status);
        }
    }

    @Override public void close() {
        synchronized (PlaybackEffects.class) {
            LIVE.remove(this);
            if (equalizer != null) { equalizer.release(); equalizer = null; }
            if (!communication) announce(AudioEffect.ACTION_CLOSE_AUDIO_EFFECT_CONTROL_SESSION);
        }
    }
}
