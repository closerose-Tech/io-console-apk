package com.aic.ioconsole;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.os.Bundle;
import android.provider.Settings;
import android.view.KeyEvent;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Toast;

import androidx.webkit.WebViewAssetLoader;
import androidx.webkit.WebViewClientCompat;

/**
 * 研华I/O卡检测控制台 —— 安卓壳。
 * 用 WebViewAssetLoader 以 https 域名加载打包在 assets/ 内的控制台页面,
 * 页面逻辑(web/index.html + app.js)与 PWA 版完全同一份。
 * JS 桥 AndroidBridge 提供:
 *   machineCode(): 设备机器码(ANDROID_ID), 供激活页计算校验码
 *   toast(msg):    原生提示
 */
public class MainActivity extends Activity {

    private static final String START_URL =
            "https://appassets.androidplatform.net/assets/index.html";

    private WebView web;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        final WebViewAssetLoader assetLoader = new WebViewAssetLoader.Builder()
                .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this))
                .build();

        web = new WebView(this);
        WebSettings st = web.getSettings();
        st.setJavaScriptEnabled(true);
        st.setDomStorageEnabled(true);          // 激活状态/服务地址持久化
        st.setDatabaseEnabled(true);
        st.setAllowFileAccess(false);
        web.setBackgroundColor(0xFFECEFF3);
        web.setWebViewClient(new WebViewClientCompat() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view,
                                                              WebResourceRequest request) {
                return assetLoader.shouldInterceptRequest(request.getUrl());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view,
                                                    WebResourceRequest request) {
                return false;   // 允许页内跳转(如下载报告)
            }
        });
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(START_URL);
        }
        setContentView(web);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
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
