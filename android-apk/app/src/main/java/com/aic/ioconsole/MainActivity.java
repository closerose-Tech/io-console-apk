package com.aic.ioconsole;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Context;
import android.content.res.AssetManager;
import android.os.Bundle;
import android.provider.Settings;
import android.view.KeyEvent;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Toast;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * 研华I/O卡检测控制台 —— 安卓壳。
 *
 * 架构: Chaquopy 在手机本地运行桥接服务(与桌面/PWA 版同一份 Python 代码),
 * WebView 加载 http://127.0.0.1:8080/。用户只需填写目标主机 SSH 信息即可
 * 直接连接检测, 无需任何中间服务器。
 *
 * JS 桥 AndroidBridge 提供:
 *   machineCode(): 设备机器码(ANDROID_ID), 供激活页计算校验码
 *   toast(msg):    原生提示
 */
public class MainActivity extends Activity {

    private static final String CONSOLE_URL = "http://127.0.0.1:8080/";

    private WebView web;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // 1. 把打包在 assets/web 的界面文件解到内部存储, 供本地桥接服务读取
        File webDir = extractWebAssets(this);

        // 2. 启动内嵌 Python 桥接服务
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(getApplicationContext()));
        }
        Python py = Python.getInstance();
        py.getModule("apk_main").callAttr("start",
                getFilesDir().getAbsolutePath(), webDir.getAbsolutePath());

        // 3. WebView 加载本地控制台
        web = new WebView(this);
        WebSettings st = web.getSettings();
        st.setJavaScriptEnabled(true);
        st.setDomStorageEnabled(true);          // 激活状态/历史命令持久化
        st.setDatabaseEnabled(true);
        web.setBackgroundColor(0xFFECEFF3);
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
            setContentView(web);
        } else {
            setContentView(web);
            waitForServer(() -> web.loadUrl(CONSOLE_URL));
        }
    }

    /** 轮询本地服务就绪后回调(最多等 20 秒) */
    private void waitForServer(final Runnable ready) {
        new Thread(new Runnable() {
            @Override
            public void run() {
                long deadline = System.currentTimeMillis() + 20000;
                while (System.currentTimeMillis() < deadline) {
                    HttpURLConnection conn = null;
                    try {
                        conn = (HttpURLConnection) new URL(CONSOLE_URL + "api/ping")
                                .openConnection();
                        conn.setConnectTimeout(800);
                        conn.setReadTimeout(800);
                        if (conn.getResponseCode() == 200) {
                            runOnUiThread(ready);
                            return;
                        }
                    } catch (IOException ignored) {
                        // 服务未就绪, 继续等
                    } finally {
                        if (conn != null) conn.disconnect();
                    }
                    try { Thread.sleep(300); } catch (InterruptedException e) { return; }
                }
                runOnUiThread(ready);   // 超时也加载, 让页面显示"服务断开"
            }
        }).start();
    }

    /** 解压 assets/web/** 到 filesDir/web, 返回目标目录 */
    private static File extractWebAssets(Context ctx) {
        File outDir = new File(ctx.getFilesDir(), "web");
        AssetManager am = ctx.getAssets();
        try {
            copyAssetsDir(am, "web", outDir);
        } catch (IOException e) {
            throw new RuntimeException("界面资产解压失败", e);
        }
        return outDir;
    }

    private static void copyAssetsDir(AssetManager am, String assetPath, File outDir)
            throws IOException {
        outDir.mkdirs();
        String[] names = am.list(assetPath);
        if (names == null || names.length == 0) {
            copyFile(am, assetPath, outDir);
            return;
        }
        for (String name : names) {
            String child = assetPath + "/" + name;
            String[] sub = am.list(child);
            if (sub != null && sub.length > 0) {
                copyAssetsDir(am, child, new File(outDir, name));
            } else {
                copyFile(am, child, outDir);
            }
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
