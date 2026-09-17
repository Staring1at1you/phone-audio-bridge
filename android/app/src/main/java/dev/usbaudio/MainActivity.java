package dev.usbaudio;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.graphics.Color;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.ArrayAdapter;
import android.widget.AdapterView;
import android.media.audiofx.AudioEffect;
import android.provider.Settings;

public final class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView status;
    private Button start;
    private Button stop;
    private final Runnable refresh = new Runnable() {
        @Override public void run() {
            String updated = AudioService.status + "\n\n电脑音频  "
                    + (AudioService.playbackConnected ? "已连接" : "未连接")
                    + "\n手机麦克风  " + (AudioService.micConnected ? "已连接" : "未连接")
                    + "\n" + AudioService.playbackModeStatus
                    + "\n" + AudioService.qualityStatus
                    + "\n" + AudioService.aecStatus
                    + "\n" + PlaybackEffects.currentStatus();
            if (!updated.contentEquals(status.getText())) status.setText(updated);
            start.setEnabled(!AudioService.running);
            stop.setEnabled(AudioService.running);
            handler.postDelayed(this, 500);
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(dp(24), dp(32), dp(24), dp(24));
        layout.setBackgroundColor(Color.rgb(247, 249, 250));
        ScrollView scroll = new ScrollView(this);
        scroll.addView(layout);
        setContentView(scroll);
        PlaybackEffects.load(this);
        TextView title = label("Phone Audio Bridge", 28);
        title.setTextColor(Color.rgb(20, 100, 88));
        layout.addView(title);
        layout.addView(label("USB / Wi-Fi · PCM 音频桥", 14));
        status = label("已停止", 18);
        status.setPadding(0, dp(32), 0, dp(24));
        layout.addView(status);
        start = new Button(this);
        start.setText("启动音频");
        start.setOnClickListener(view -> requestStart());
        layout.addView(start);
        stop = new Button(this);
        stop.setText("停止");
        stop.setOnClickListener(view -> stopService(new Intent(this, AudioService.class)));
        layout.addView(stop);
        layout.addView(label("播放音量", 16));
        SeekBar volume = new SeekBar(this);
        volume.setMax(100);
        volume.setProgress(Math.round(AudioService.volume * 100));
        volume.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(SeekBar bar, int value, boolean user) {
                AudioService.volume = value / 100f;
            }
            @Override public void onStartTrackingTouch(SeekBar bar) { }
            @Override public void onStopTrackingTouch(SeekBar bar) { }
        });
        layout.addView(volume);
        Switch mute = new Switch(this);
        mute.setText("麦克风静音");
        mute.setChecked(AudioService.micMuted);
        mute.setOnCheckedChangeListener((button, checked) -> AudioService.micMuted = checked);
        layout.addView(mute);
        Switch echo = new Switch(this);
        echo.setText("免提回声消除（AEC）");
        echo.setChecked(AudioService.echoCancellation);
        echo.setOnCheckedChangeListener((button, checked) -> AudioService.setEchoCancellation(checked));
        layout.addView(echo);
        layout.addView(label("播放音效（音乐 / 电影等为标准 EQ）", 16));
        Spinner effects = new Spinner(this);
        ArrayAdapter<String> adapter = new ArrayAdapter<>(this,
                android.R.layout.simple_spinner_item, PlaybackEffects.LABELS);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        effects.setAdapter(adapter);
        effects.setSelection(PlaybackEffects.profile);
        effects.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                PlaybackEffects.select(MainActivity.this, position);
            }
            @Override public void onNothingSelected(AdapterView<?> parent) { }
        });
        layout.addView(effects);
        layout.addView(label(PlaybackEffects.availableVendors(), 13));
        Button systemEffects = new Button(this);
        systemEffects.setText("打开系统音效 / Dolby 设置");
        systemEffects.setOnClickListener(view -> {
            Intent panel = new Intent(AudioEffect.ACTION_DISPLAY_AUDIO_EFFECT_CONTROL_PANEL)
                    .putExtra(AudioEffect.EXTRA_AUDIO_SESSION, PlaybackEffects.currentSession())
                    .putExtra(AudioEffect.EXTRA_PACKAGE_NAME, getPackageName());
            try { startActivity(panel); }
            catch (android.content.ActivityNotFoundException | SecurityException error) {
                startActivity(new Intent(Settings.ACTION_SOUND_SETTINGS));
            }
        });
        layout.addView(systemEffects);
        if (Build.VERSION.SDK_INT >= 35) {
            layout.setOnApplyWindowInsetsListener((view, insets) -> {
                android.graphics.Insets bars = insets.getInsets(
                        android.view.WindowInsets.Type.systemBars());
                view.setPadding(dp(24), bars.top + dp(16), dp(24), bars.bottom + dp(16));
                return insets;
            });
        }
        if (getIntent().getBooleanExtra("start_audio", false)) requestStart();
    }

    private void requestStart() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            String[] permissions = Build.VERSION.SDK_INT >= 33
                    ? new String[]{Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS}
                    : new String[]{Manifest.permission.RECORD_AUDIO};
            requestPermissions(permissions, 1);
        } else {
            startForegroundService(new Intent(this, AudioService.class));
        }
    }

    @Override public void onRequestPermissionsResult(int code, String[] permissions, int[] grants) {
        super.onRequestPermissionsResult(code, permissions, grants);
        if (code == 1 && checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED) {
            requestStart();
        }
    }

    @Override public void onResume() {
        super.onResume();
        handler.post(refresh);
    }

    @Override public void onPause() {
        handler.removeCallbacks(refresh);
        super.onPause();
    }

    private TextView label(String text, int size) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextSize(size);
        view.setTextColor(Color.rgb(35, 42, 45));
        return view;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
