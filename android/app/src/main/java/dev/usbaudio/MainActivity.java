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

public final class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView status;
    private Button start;
    private Button stop;
    private final Runnable refresh = new Runnable() {
        @Override public void run() {
            status.setText(AudioService.status + "\n\n电脑音频  "
                    + (AudioService.playbackConnected ? "已连接" : "未连接")
                    + "\n手机麦克风  " + (AudioService.micConnected ? "已连接" : "未连接")
                    + "\n" + AudioService.playbackModeStatus
                    + "\n" + AudioService.qualityStatus
                    + "\n" + AudioService.aecStatus);
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
        setContentView(layout);
        TextView title = label("Phone Audio Bridge", 28);
        title.setTextColor(Color.rgb(20, 100, 88));
        layout.addView(title);
        layout.addView(label("48 kHz · PCM 16-bit", 14));
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
