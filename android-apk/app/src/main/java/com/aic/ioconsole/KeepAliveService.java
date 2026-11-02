package com.aic.ioconsole;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

/**
 * 前台保活服务: APP 退到后台 / 锁屏 / 收起时, 保持进程与网络存活,
 * 使与目标主机的 SSH 连接不被系统休眠或路由器空闲超时断开。
 *
 * - 常驻低优先级通知(可在系统设置里关掉通知显示, 不影响保活);
 * - PARTIAL_WAKE_LOCK + 高性能 WifiLock 防止 Doze 模式掐断网络;
 * - START_STICKY: 进程万一被回收, 系统会尝试拉起服务(此时连接需重新建立)。
 * - stopWithTask=false: 从最近任务划掉 APP 后服务继续运行, 回来连接仍在。
 */
public class KeepAliveService extends Service {

    private static final String CHANNEL_ID = "ioconsole-keepalive";
    private static final int NOTI_ID = 1;

    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;

    @Override
    public void onCreate() {
        super.onCreate();
        startNotice();
        acquireLocks();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        releaseLocks();
        super.onDestroy();
    }

    /* ------------------------------------------------------------ 通知 */
    private void startNotice() {
        NotificationManager nm =
                (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID,
                    "连接保持", NotificationManager.IMPORTANCE_MIN);
            ch.setDescription("保持与目标主机的连接");
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        b.setContentTitle("I/O 卡检测控制台")
                .setContentText("正在保持与目标主机的连接")
                .setSmallIcon(getApplicationInfo().icon)
                .setOngoing(true);
        Notification noti = b.build();
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTI_ID, noti,
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            } else {
                startForeground(NOTI_ID, noti);
            }
        } catch (Exception ignored) {
            // 通知权限被关等情况: 服务仍在运行, 只是没通知
        }
    }

    /* ------------------------------------------------------------ 锁 */
    private void acquireLocks() {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "aic:ioconsole");
            wakeLock.setReferenceCounted(false);
            wakeLock.acquire();
        } catch (Exception ignored) { }
        try {
            Object wmObj = getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (wmObj instanceof WifiManager) {
                WifiManager wm = (WifiManager) wmObj;
                int mode = Build.VERSION.SDK_INT >= 34
                        ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                        : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
                wifiLock = wm.createWifiLock(mode, "aic:ioconsole-wifi");
                wifiLock.setReferenceCounted(false);
                wifiLock.acquire();
            }
        } catch (Exception ignored) { }
    }

    private void releaseLocks() {
        try { if (wakeLock != null && wakeLock.isHeld()) wakeLock.release(); }
        catch (Exception ignored) { }
        try { if (wifiLock != null && wifiLock.isHeld()) wifiLock.release(); }
        catch (Exception ignored) { }
    }
}
