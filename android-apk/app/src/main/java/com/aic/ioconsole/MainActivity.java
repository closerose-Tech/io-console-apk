package com.aic.ioconsole;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.res.AssetManager;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.view.KeyEvent;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Toast;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * 研华I/O卡检测控制台 —— 安卓壳。
 *
 * 架构: Chaquopy 在手机本地运行桥接服务(与桌面/PWA 版同一份 Python 代码),
 * WebView 加载 http://127.0.0.1:8080/。用户只需填写目标主机 SSH 信息即可
 * 直接连接检测, 无需任何中间服务器。
 *
 * v2.3.2: 启动过程分阶段实时显示(含每步耗时); 首次运行 Python 解压/模块
 *         导入较慢(10~60 秒), 进度页让用户看到进展; 60 秒看门狗超时后
 *         显示诊断信息(Android 版本/架构/卡住的阶段), 不再无限灰屏。
 */
public class MainActivity extends Activity {

    private static final String CONSOLE_URL = "http://127.0.0.1:8080/";

    private WebView web;
    private long t0;                 // 启动计时
    private final StringBuilder stages = new StringBuilder();   // 已完成阶段记录(供错误页显示)

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        web = new WebView(this);
        WebSettings st = web.getSettings();
        st.setJavaScriptEnabled(true);
        st.setDomStorageEnabled(true);          // 激活状态/历史命令持久化
        st.setDatabaseEnabled(true);
        web.setBackgroundColor(0xFFECEFF3);
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");
        setContentView(web);

        // 前台保活: 退后台/锁屏时保持进程与网络存活, SSH 连接不掉
        startKeepAlive();

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
            return;
        }
        showSplash();
        t0 = System.currentTimeMillis();
        startInBackground();
    }

    /* ------------------------------------------------------------ 连接保活 */

    /** 启动前台保活服务(常驻通知 + CPU/Wi-Fi 锁)。失败不影响主流程。 */
    private void startKeepAlive() {
        if (Build.VERSION.SDK_INT >= 33) {
            try {
                requestPermissions(new String[]{
                        android.Manifest.permission.POST_NOTIFICATIONS}, 1);
            } catch (Exception ignored) { }
        }
        try {
            Intent it = new Intent(this, KeepAliveService.class);
            if (Build.VERSION.SDK_INT >= 26) {
                startForegroundService(it);
            } else {
                startService(it);
            }
        } catch (Exception ignored) { }
    }

    /* ------------------------------------------------------------ 启动进度页 */

    private static final String SPLASH_HTML =
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        + "<style>body{margin:0;background:#eceff3;color:#37474f;"
        + "font-family:sans-serif;display:flex;flex-direction:column;"
        + "align-items:center;justify-content:center;height:100vh}"
        + "h1{font-size:20px;letter-spacing:2px;margin:0 0 6px}"
        + ".sub{font-size:13px;color:#78909c;margin-bottom:26px}"
        + "#log{width:82%;max-width:360px;font-size:14px;line-height:2.1;"
        + "font-family:sans-serif}"
        + "#log div{border-bottom:1px solid #cfd8dc;padding:0 4px;"
        + "word-break:break-all}"
        + ".spin{color:#00695c}</style></head>"
        + "<body><h1>研华I/O卡检测控制台</h1>"
        + "<div class='sub' id='elapsed'>正在启动…</div>"
        + "<div id='log'></div>"
        + "<script>function addStage(s){var d=document.createElement('div');"
        + "d.textContent=s;document.getElementById('log').appendChild(d);}"
        + "function setElapsed(s){document.getElementById('elapsed').textContent=s;}"
        + "</script></body></html>";

    private void showSplash() {
        // loadDataWithBaseURL 才能正确渲染中文
        web.loadDataWithBaseURL(null, SPLASH_HTML, "text/html", "utf-8", null);
    }

    /** 启动线程 → 主线程: 在进度页追加一行 */
    private void stage(final String line) {
        stages.append(line).append('\n');
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (web != null) web.evaluateJavascript(
                        "addStage(" + JSONObject.quote(line) + ")", null);
            }
        });
    }

    private void elapsed(final String text) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (web != null) web.evaluateJavascript(
                        "setElapsed(" + JSONObject.quote(text) + ")", null);
            }
        });
    }

    private String secs() {
        return String.format("%.1f 秒", (System.currentTimeMillis() - t0) / 1000.0);
    }

    /* ------------------------------------------------------------ 启动流程 */

    /** 后台完成: 资产解压 → Python 初始化 → 服务就绪 → 加载界面 */
    private void startInBackground() {
        final Context app = getApplicationContext();
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    // 1. 界面资产
                    stage("▸ 准备界面资产…");
                    File webDir = extractWebAssets(app);
                    stage("✓ 界面资产就绪 (" + secs() + ")");

                    // 1.5 内置驱动安装包 (assets/driver/*.run → filesDir/uploads)
                    stage("▸ 准备内置驱动安装包…");
                    extractBuiltinDriver(app);
                    stage("✓ 内置驱动就绪 (" + secs() + ")");

                    // 2. Python 引擎初始化(首次运行要解压标准库, 较慢)
                    stage("▸ 初始化 Python 引擎(首次运行约 10~60 秒)…");
                    if (!Python.isStarted()) {
                        Python.start(new AndroidPlatform(app));
                    }
                    stage("✓ Python 引擎就绪 (" + secs() + ")");

                    // 3. 导入桥接模块(含 paramiko/加密库, 首次较慢)
                    stage("▸ 加载桥接模块…");
                    Python py = Python.getInstance();
                    py.getModule("apk_main");       // 触发 import bridge/paramiko
                    stage("✓ 桥接模块加载完成 (" + secs() + ")");

                    // 4. 启动本地服务
                    stage("▸ 启动本地控制台服务…");
                    py.getModule("apk_main").callAttr("start",
                            app.getFilesDir().getAbsolutePath(),
                            webDir.getAbsolutePath());
                    stage("✓ 服务已启动 (" + secs() + ")");

                    // 5. 等待服务就绪后加载界面(带看门狗)
                    waitForServer();
                } catch (Throwable t) {
                    showError(t);
                }
            }
        }, "app-startup").start();
    }

    /** 轮询本地服务就绪; 60 秒未就绪则显示诊断页 */
    private void waitForServer() {
        long deadline = System.currentTimeMillis() + 60000;
        long lastReport = 0;
        while (System.currentTimeMillis() < deadline) {
            HttpURLConnection conn = null;
            try {
                conn = (HttpURLConnection) new URL(CONSOLE_URL + "api/ping")
                        .openConnection();
                conn.setConnectTimeout(800);
                conn.setReadTimeout(800);
                if (conn.getResponseCode() == 200) {
                    elapsed("启动完成 " + secs());
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            if (web != null) web.loadUrl(CONSOLE_URL);
                        }
                    });
                    return;
                }
            } catch (IOException ignored) {
                // 服务未就绪, 继续等
            } finally {
                if (conn != null) conn.disconnect();
            }
            long now = System.currentTimeMillis();
            if (now - lastReport > 2000) {
                lastReport = now;
                final long s = (now - t0) / 1000;
                elapsed("正在等待本地服务就绪… " + s + " 秒");
            }
            try { Thread.sleep(300); } catch (InterruptedException e) { return; }
        }
        // 看门狗超时
        stage("✗ 本地服务 60 秒未就绪");
        showError(new RuntimeException("本地服务启动超时(60秒)"));
    }

    /* ------------------------------------------------------------ 错误展示 */

    /** 启动失败/超时: 页面显示阶段记录 + 错误堆栈 + 设备信息(不闪退) */
    private void showError(Throwable t) {
        StringWriter sw = new StringWriter();
        t.printStackTrace(new PrintWriter(sw));
        String trace = esc(sw.toString());
        String dev = esc("Android " + Build.VERSION.RELEASE + " (API " + Build.VERSION.SDK_INT
                + "), " + Build.SUPPORTED_ABIS[0] + ", 型号 " + Build.MANUFACTURER + " " + Build.MODEL);
        String stageTxt = esc(stages.toString());
        final String html = "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>body{margin:0;background:#fff3f0;color:#b3261e;"
                + "font-family:monospace;font-size:12px;padding:14px;"
                + "word-break:break-all;white-space:pre-wrap}"
                + "h2{font-family:sans-serif;font-size:18px;margin:4px 0 10px}"
                + ".dev{color:#8d6e63;margin:8px 0}</style></head>"
                + "<body><h2>启动失败，请截图反馈给管理员</h2>"
                + "<div class='dev'>设备: " + dev + "</div>"
                + "<div>启动过程:\n" + stageTxt + "</div>"
                + "<div>错误:\n" + trace + "</div></body></html>";
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (web != null) {
                    web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
                }
            }
        });
    }

    private static String esc(String s) {
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    /* ------------------------------------------------------------ 资产解压 */

    /** 解压 assets/web/* 到 filesDir/web, 返回目标目录 */
    private static File extractWebAssets(Context ctx) {
        File outDir = new File(ctx.getFilesDir(), "web");
        AssetManager am = ctx.getAssets();
        try {
            copyFromManifest(am, outDir);
        } catch (IOException e) {
            throw new RuntimeException("界面资产解压失败", e);
        }
        return outDir;
    }

    /**
     * 按清单文件(web/filelist.txt, 由 sync_apk_assets.py 生成)逐个复制。
     * 不用 AssetManager.list(): 在部分新系统(如 Android 17)上对 assets
     * 目录返回空, 会导致把目录当文件打开报 EISDIR。
     */
    private static void copyFromManifest(AssetManager am, File outDir)
            throws IOException {
        outDir.mkdirs();
        java.util.List<String> names = new java.util.ArrayList<>();
        java.io.BufferedReader reader = new java.io.BufferedReader(
                new java.io.InputStreamReader(am.open("web/filelist.txt"), "UTF-8"));
        try {
            String line;
            while ((line = reader.readLine()) != null) {
                line = line.trim();
                if (!line.isEmpty() && !line.startsWith("#")) names.add(line);
            }
        } finally {
            try { reader.close(); } catch (IOException ignored) { }
        }
        if (names.isEmpty()) throw new IOException("资产清单 filelist.txt 为空");
        for (String name : names) {
            copyFile(am, "web/" + name, new File(outDir, name));
        }
    }

    /**
     * 解压 assets/driver/ 下的内置驱动安装包到 filesDir/uploads/,
     * 桥接服务的 list_installers() 会自动扫描该目录, 驱动页即可直接选用。
     * 清单格式(filelist.txt, 由 sync_apk_assets.py 生成): "文件名 大小",
     * 已存在且大小一致时跳过, 避免每次启动重复复制 20 多 MB。
     */
    private static void extractBuiltinDriver(Context ctx) {
        AssetManager am = ctx.getAssets();
        File upDir = new File(ctx.getFilesDir(), "uploads");
        if (!upDir.exists() && !upDir.mkdirs()) {
            throw new RuntimeException("无法创建 uploads 目录");
        }
        java.util.List<String[]> entries = new java.util.ArrayList<>();
        java.io.BufferedReader reader = null;
        try {
            reader = new java.io.BufferedReader(
                    new java.io.InputStreamReader(am.open("driver/filelist.txt"), "UTF-8"));
            String line;
            while ((line = reader.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#")) continue;
                String[] parts = line.split("\\s+");
                if (parts.length >= 1) {
                    entries.add(new String[]{parts[0],
                            parts.length > 1 ? parts[1] : "0"});
                }
            }
        } catch (IOException e) {
            throw new RuntimeException("内置驱动清单读取失败", e);
        } finally {
            if (reader != null) try { reader.close(); } catch (IOException ignored) { }
        }
        try {
            for (String[] entry : entries) {
                File dst = new File(upDir, entry[0]);
                long expect = 0;
                try { expect = Long.parseLong(entry[1]); } catch (NumberFormatException ignored) { }
                if (dst.isFile() && dst.length() == expect && expect > 0) continue;
                copyFile(am, "driver/" + entry[0], dst);
                if (expect > 0 && dst.length() != expect) {
                    throw new IOException("内置驱动解压不完整: " + entry[0]
                            + " 期望 " + expect + " 实际 " + dst.length());
                }
            }
        } catch (IOException e) {
            throw new RuntimeException("内置驱动解压失败", e);
        }
    }

    private static void copyFile(AssetManager am, String assetPath, File outFile)
            throws IOException {
        InputStream in = null;
        OutputStream out = null;
        try {
            in = am.open(assetPath);
            FileOutputStream fout = new FileOutputStream(outFile);
            out = fout;
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        } finally {
            if (in != null) try { in.close(); } catch (IOException ignored) { }
            if (out != null) try { out.close(); } catch (IOException ignored) { }
        }
    }

    /* ------------------------------------------------------------ 其他 */

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    /** 提供给页面 JS 的原生桥 */
    public class Bridge {
        @JavascriptInterface
        public String machineCode() {
            String id = Settings.Secure.getString(getContentResolver(),
                    Settings.Secure.ANDROID_ID);
            return (id == null || id.isEmpty()) ? "UNKNOWN-DEVICE" : id;
        }

        @JavascriptInterface
        public void toast(final String msg) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    Toast.makeText(MainActivity.this, msg, Toast.LENGTH_SHORT).show();
                }
            });
        }
    }
}
