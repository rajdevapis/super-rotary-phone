from flask import Flask, request, jsonify, render_template, Response
import os, re, json, base64, uuid, urllib.request, urllib.error, io, zipfile

app = Flask(__name__)

@app.errorhandler(Exception)
def handle_any_error(e):
    """Kabhi bhi koi unexpected crash ho, HTML error page ki jagah
    hamesha JSON bhejo — taaki frontend ka res.json() kabhi fail na ho."""
    import traceback
    code = getattr(e, "code", 500)
    if not isinstance(code, int):
        code = 500
    traceback.print_exc()
    return jsonify({"success": False, "error": f"Server error: {e}"}), code

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "apk-builds")

AD_SESSIONS = {}  # api_key -> session dict

PKG_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$')

def gh(method, path, body=None):
    url  = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {"message": f"GitHub returned status {e.code}"}, e.code
    except urllib.error.URLError as e:
        return {"message": f"GitHub tak connect nahi ho paya: {e.reason}"}, 502
    except Exception as e:
        return {"message": f"GitHub request error: {e}"}, 502

def b64(t): return base64.b64encode(t.encode()).decode()
def sanitize(n): return re.sub(r'[^a-zA-Z0-9_\-]', '_', n)
def valid_pkg(p): return bool(p and PKG_RE.match(p))

# ══════════════════════════════════════════════════════════════════════════════
#  ANDROID FILE GENERATORS
# ══════════════════════════════════════════════════════════════════════════════

def make_main_activity_unity(pkg, unity_id, content_type, content_value,
                              has_rewarded, has_interstitial, auto_show, test_mode):
    if content_type == "url":
        load_line = f'webView.loadUrl("{content_value}");'
    else:
        esc = (content_value.replace('\\','\\\\').replace('"','\\"')
                            .replace('\n','\\n').replace('\r',''))
        load_line = f'webView.loadData("{esc}", "text/html", "UTF-8");'

    r_place = '"Rewarded_Android"'     if has_rewarded     else 'null'
    i_place = '"Interstitial_Android"' if has_interstitial else 'null'
    r_vis   = "View.VISIBLE"           if has_rewarded     else "View.GONE"
    i_vis   = "View.VISIBLE"           if has_interstitial else "View.GONE"
    auto_j  = "true" if auto_show  else "false"
    test_j  = "true" if test_mode  else "false"

    return f"""package {pkg};

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.Toast;
import com.unity3d.ads.IUnityAdsInitializationListener;
import com.unity3d.ads.IUnityAdsLoadListener;
import com.unity3d.ads.IUnityAdsShowListener;
import com.unity3d.ads.UnityAds;
import com.unity3d.ads.UnityAdsShowOptions;

public class MainActivity extends Activity implements IUnityAdsInitializationListener {{

    private static final String TAG              = "UnityAdsApp";
    private static final String UNITY_GAME_ID   = "{unity_id}";
    private static final boolean TEST_MODE       = {test_j};
    private static final String REWARDED         = {r_place};
    private static final String INTERSTITIAL     = {i_place};
    private static final boolean AUTO_SHOW       = {auto_j};
    private static final int RETRY_MS            = 5000;
    private static final int FAST_RETRY_MS       = 1200;

    private WebView webView;
    private Button  btnRewarded, btnInterstitial;
    private boolean rewardedReady = false, interstitialReady = false, sdkReady = false;
    private boolean loadingR = false, loadingI = false;
    private boolean pendingR = false, pendingI = false;
    private boolean autoFired = false;
    private int     autoAttempts = 0;
    private final Handler handler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {{
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView         = findViewById(R.id.webView);
        btnRewarded     = findViewById(R.id.btnRewarded);
        btnInterstitial = findViewById(R.id.btnInterstitial);

        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        ws.setCacheMode(WebSettings.LOAD_DEFAULT);
        ws.setSupportMultipleWindows(true);
        ws.setJavaScriptCanOpenWindowsAutomatically(true);

        // JS Bridge: window.NativeAds.showRewarded() / showInterstitial()
        webView.addJavascriptInterface(new AdBridge(), "NativeAds");

        // URL Bridge: <a href="ads://show_rewarded"> or <a href="ads://show_interstitial">
        webView.setWebViewClient(new WebViewClient() {{
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {{
                if (url.startsWith("ads://show_rewarded"))    {{ triggerRewarded();     return true; }}
                if (url.startsWith("ads://show_interstitial")){{ triggerInterstitial(); return true; }}
                return false;
            }}
        }});

        webView.setWebChromeClient(new WebChromeClient() {{
            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog,
                    boolean isUserGesture, android.os.Message resultMsg) {{
                WebView.HitTestResult r = view.getHitTestResult();
                String u = r != null ? r.getExtra() : null;
                if (u != null) view.loadUrl(u);
                return false;
            }}
        }});

        {load_line}

        btnRewarded.setVisibility({r_vis});
        btnInterstitial.setVisibility({i_vis});

        // Init Unity Ads immediately
        UnityAds.initialize(this, UNITY_GAME_ID, TEST_MODE, this);

        // 12s timeout warning
        handler.postDelayed(() -> {{
            if (!sdkReady) Toast.makeText(this,
                "Ads SDK ready nahi — Game ID/internet check karo", Toast.LENGTH_LONG).show();
        }}, 12000);

        btnRewarded.setOnClickListener(v -> {{
            if (!sdkReady) {{ Toast.makeText(this,"SDK init ho raha...",Toast.LENGTH_SHORT).show(); return; }}
            if (rewardedReady) showRewarded();
            else {{ pendingR = true; Toast.makeText(this,"Loading, abhi aata hai...",Toast.LENGTH_SHORT).show(); loadRewarded(); }}
        }});

        btnInterstitial.setOnClickListener(v -> {{
            if (!sdkReady) {{ Toast.makeText(this,"SDK init ho raha...",Toast.LENGTH_SHORT).show(); return; }}
            if (interstitialReady) showInterstitial();
            else {{ pendingI = true; Toast.makeText(this,"Loading, abhi aata hai...",Toast.LENGTH_SHORT).show(); loadInterstitial(); }}
        }});
    }}

    @Override
    public void onInitializationComplete() {{
        sdkReady = true;
        if (REWARDED != null)     loadRewarded();
        if (INTERSTITIAL != null) loadInterstitial();
        if (AUTO_SHOW) maybeAutoShow();
    }}

    @Override
    public void onInitializationFailed(UnityAds.UnityAdsInitializationError e, String m) {{
        Log.e(TAG, "Init failed: " + e + " " + m);
        handler.postDelayed(() -> UnityAds.initialize(this, UNITY_GAME_ID, TEST_MODE, this), RETRY_MS);
    }}

    private void maybeAutoShow() {{
        if (autoFired || autoAttempts++ > 20) return;
        boolean rOk = REWARDED == null     || rewardedReady;
        boolean iOk = INTERSTITIAL == null || interstitialReady;
        if (!rOk || !iOk) {{ handler.postDelayed(this::maybeAutoShow, 800); return; }}
        autoFired = true;
        if (REWARDED != null) {{
            showRewarded();
            if (INTERSTITIAL != null) handler.postDelayed(this::showInterstitial, 4000);
        }} else if (INTERSTITIAL != null) showInterstitial();
    }}

    private void triggerRewarded()     {{ runOnUiThread(() -> {{ if (rewardedReady)     showRewarded();     else {{ pendingR = true; loadRewarded(); }} }}); }}
    private void triggerInterstitial() {{ runOnUiThread(() -> {{ if (interstitialReady) showInterstitial(); else {{ pendingI = true; loadInterstitial(); }} }}); }}

    private class AdBridge {{
        @JavascriptInterface public void showRewarded()     {{ triggerRewarded(); }}
        @JavascriptInterface public void showInterstitial() {{ triggerInterstitial(); }}
    }}

    private void loadRewarded() {{
        if (REWARDED == null || !sdkReady || loadingR) return;
        loadingR = true;
        UnityAds.load(REWARDED, new IUnityAdsLoadListener() {{
            public void onUnityAdsAdLoaded(String p)    {{ loadingR=false; rewardedReady=true; if(pendingR){{pendingR=false;showRewarded();}} }}
            public void onUnityAdsFailedToLoad(String p, UnityAds.UnityAdsLoadError e, String m) {{
                loadingR=false; rewardedReady=false;
                handler.postDelayed(()->loadRewarded(), pendingR?FAST_RETRY_MS:RETRY_MS);
            }}
        }});
    }}

    private void loadInterstitial() {{
        if (INTERSTITIAL == null || !sdkReady || loadingI) return;
        loadingI = true;
        UnityAds.load(INTERSTITIAL, new IUnityAdsLoadListener() {{
            public void onUnityAdsAdLoaded(String p)    {{ loadingI=false; interstitialReady=true; if(pendingI){{pendingI=false;showInterstitial();}} }}
            public void onUnityAdsFailedToLoad(String p, UnityAds.UnityAdsLoadError e, String m) {{
                loadingI=false; interstitialReady=false;
                handler.postDelayed(()->loadInterstitial(), pendingI?FAST_RETRY_MS:RETRY_MS);
            }}
        }});
    }}

    private void showRewarded() {{
        rewardedReady = false;
        UnityAds.show(this, REWARDED, new UnityAdsShowOptions(), new IUnityAdsShowListener() {{
            public void onUnityAdsShowFailure(String p,UnityAds.UnityAdsShowError e,String m) {{ loadRewarded(); }}
            public void onUnityAdsShowStart(String p)  {{}}
            public void onUnityAdsShowClick(String p)  {{}}
            public void onUnityAdsShowComplete(String p,UnityAds.UnityAdsShowCompletionState s) {{
                if(s==UnityAds.UnityAdsShowCompletionState.COMPLETED)
                    Toast.makeText(MainActivity.this,"🎁 Reward mila!",Toast.LENGTH_SHORT).show();
                loadRewarded();
            }}
        }});
    }}

    private void showInterstitial() {{
        interstitialReady = false;
        UnityAds.show(this, INTERSTITIAL, new UnityAdsShowOptions(), new IUnityAdsShowListener() {{
            public void onUnityAdsShowFailure(String p,UnityAds.UnityAdsShowError e,String m) {{ loadInterstitial(); }}
            public void onUnityAdsShowStart(String p)  {{}}
            public void onUnityAdsShowClick(String p)  {{}}
            public void onUnityAdsShowComplete(String p,UnityAds.UnityAdsShowCompletionState s) {{ loadInterstitial(); }}
        }});
    }}

    @Override protected void onResume() {{
        super.onResume();
        if (sdkReady) {{ if(!rewardedReady) loadRewarded(); if(!interstitialReady) loadInterstitial(); }}
    }}
    @Override protected void onDestroy() {{ handler.removeCallbacksAndMessages(null); super.onDestroy(); }}
    @Override public void onBackPressed() {{ if(webView.canGoBack()) webView.goBack(); else super.onBackPressed(); }}
}}
"""


def make_main_activity_startapp(pkg, startapp_id, content_type, content_value,
                                 has_banner, has_interstitial, has_rewarded, auto_show):
    if content_type == "url":
        load_line = f'webView.loadUrl("{content_value}");'
    else:
        esc = (content_value.replace('\\','\\\\').replace('"','\\"')
                            .replace('\n','\\n').replace('\r',''))
        load_line = f'webView.loadData("{esc}", "text/html", "UTF-8");'

    banner_code = ""
    if has_banner:
        banner_code = """
        // StartApp Banner
        Banner startAppBanner = new Banner(this);
        bannerLayout.addView(startAppBanner);"""

    interstitial_code = ""
    if has_interstitial:
        interstitial_code = """
        // StartApp Interstitial preload
        interstitialAd = new StartAppAd(this);
        interstitialAd.loadAd();"""

    rewarded_code = ""
    if has_rewarded:
        rewarded_code = """
        // StartApp Rewarded Video preload
        loadRewarded();"""

    auto_code = ""
    if auto_show and has_interstitial:
        auto_code = """
        // Auto show on open
        handler.postDelayed(() -> {
            if (interstitialAd != null) interstitialAd.showAd();
        }, 2000);"""

    r_vis = "View.VISIBLE" if has_rewarded     else "View.GONE"
    i_vis = "View.VISIBLE" if has_interstitial else "View.GONE"

    return f"""package {pkg};

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.Toast;
import com.startapp.sdk.adsbase.Ad;
import com.startapp.sdk.adsbase.StartAppAd;
import com.startapp.sdk.adsbase.StartAppSDK;
import com.startapp.sdk.adsbase.adlisteners.AdEventListener;
import com.startapp.sdk.adsbase.adlisteners.VideoListener;
import com.startapp.sdk.ads.banner.Banner;

public class MainActivity extends Activity {{

    private static final String APP_ID = "{startapp_id}";
    private static final int RETRY_MS  = 5000;

    private WebView webView;
    private FrameLayout bannerLayout;
    private Button btnRewarded, btnInterstitial;
    private StartAppAd interstitialAd;
    private StartAppAd rewardedAd;
    private boolean rewardedReady = false;
    private final Handler handler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {{
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main_startapp);

        webView         = findViewById(R.id.webView);
        bannerLayout    = findViewById(R.id.bannerLayout);
        btnRewarded     = findViewById(R.id.btnRewarded);
        btnInterstitial = findViewById(R.id.btnInterstitial);

        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);

        // JS Bridge: window.NativeAds.showRewarded() / showInterstitial()
        webView.addJavascriptInterface(new AdBridge(), "NativeAds");

        // URL Bridge: <a href="ads://show_rewarded"> or <a href="ads://show_interstitial">
        webView.setWebViewClient(new WebViewClient() {{
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {{
                if (url.startsWith("ads://show_rewarded"))     {{ showRewarded();     return true; }}
                if (url.startsWith("ads://show_interstitial")) {{ showInterstitial(); return true; }}
                return false;
            }}
        }});
        {load_line}

        StartAppSDK.init(this, APP_ID, false);
        {banner_code}
        {interstitial_code}
        {rewarded_code}
        {auto_code}

        if (btnRewarded != null) {{
            btnRewarded.setVisibility({r_vis});
            btnRewarded.setOnClickListener(v -> showRewarded());
        }}
        if (btnInterstitial != null) {{
            btnInterstitial.setVisibility({i_vis});
            btnInterstitial.setOnClickListener(v -> showInterstitial());
        }}
    }}

    private void showInterstitial() {{
        if (interstitialAd != null && interstitialAd.isReady()) {{
            interstitialAd.showAd();
            interstitialAd.loadAd();
        }} else {{
            Toast.makeText(this, "Ad load ho raha hai, ek pal ruko...", Toast.LENGTH_SHORT).show();
        }}
    }}

    private void loadRewarded() {{
        rewardedAd = new StartAppAd(this);
        rewardedAd.setVideoListener(new VideoListener() {{
            @Override public void onVideoCompleted() {{
                runOnUiThread(() -> Toast.makeText(MainActivity.this,
                    "🎁 Reward mila!", Toast.LENGTH_SHORT).show());
            }}
        }});
        rewardedAd.loadAd(StartAppAd.AdMode.REWARDED_VIDEO, new AdEventListener() {{
            @Override public void onReceiveAd(Ad ad)      {{ rewardedReady = true; }}
            @Override public void onFailedToReceiveAd(Ad ad) {{
                rewardedReady = false;
                handler.postDelayed(() -> loadRewarded(), RETRY_MS);
            }}
        }});
    }}

    private void showRewarded() {{
        if (rewardedAd != null && rewardedReady) {{
            rewardedAd.showAd();
            rewardedReady = false;
            handler.postDelayed(() -> loadRewarded(), 1000);
        }} else {{
            Toast.makeText(this, "Ad load ho raha hai, ek pal ruko...", Toast.LENGTH_SHORT).show();
            loadRewarded();
        }}
    }}

    private class AdBridge {{
        @JavascriptInterface public void showRewarded()     {{ runOnUiThread(MainActivity.this::showRewarded); }}
        @JavascriptInterface public void showInterstitial() {{ runOnUiThread(MainActivity.this::showInterstitial); }}
    }}

    @Override protected void onResume() {{
        super.onResume();
        if (interstitialAd != null) interstitialAd.loadAd();
        if (rewardedAd != null && !rewardedReady) loadRewarded();
    }}
    @Override protected void onDestroy() {{
        handler.removeCallbacksAndMessages(null);
        super.onDestroy();
    }}
    @Override public void onBackPressed() {{
        if (webView.canGoBack()) webView.goBack(); else super.onBackPressed();
    }}
}}
"""


def make_manifest(pkg, app_name, network="unity"):
    unity_activities = ""
    if network == "unity":
        unity_activities = """
        <activity android:name="com.unity3d.ads.adunit.AdUnitActivity"
            android:configChanges="fontScale|keyboard|keyboardHidden|locale|mnc|mcc|navigation|orientation|screenLayout|screenSize|smallestScreenSize|uiMode|touchscreen"
            android:hardwareAccelerated="true"
            android:theme="@android:style/Theme.NoTitleBar.Fullscreen"/>
        <activity android:name="com.unity3d.ads.adunit.AdUnitTransparentActivity"
            android:configChanges="fontScale|keyboard|keyboardHidden|locale|mnc|mcc|navigation|orientation|screenLayout|screenSize|smallestScreenSize|uiMode|touchscreen"
            android:hardwareAccelerated="true"
            android:theme="@android:style/Theme.Translucent.NoTitleBar.Fullscreen"/>"""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET"/>
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE"/>
    <application android:label="{app_name}" android:allowBackup="true"
        android:usesCleartextTraffic="true"
        android:theme="@style/Theme.AppCompat.NoActionBar">
        <activity android:name=".MainActivity" android:exported="true"
            android:configChanges="keyboard|keyboardHidden|orientation|screenSize">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>{unity_activities}
    </application>
</manifest>
"""


def make_layout_unity():
    return """<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent" android:layout_height="match_parent">
    <WebView android:id="@+id/webView"
        android:layout_width="match_parent" android:layout_height="match_parent"/>
    <LinearLayout android:layout_width="match_parent" android:layout_height="wrap_content"
        android:layout_gravity="bottom" android:orientation="horizontal"
        android:padding="8dp" android:background="#CC000000">
        <Button android:id="@+id/btnRewarded"
            android:layout_width="0dp" android:layout_height="wrap_content"
            android:layout_weight="1" android:text="🎁 Watch Ad"
            android:textColor="#FFFFFF" android:backgroundTint="#4CAF50" android:layout_marginEnd="4dp"/>
        <Button android:id="@+id/btnInterstitial"
            android:layout_width="0dp" android:layout_height="wrap_content"
            android:layout_weight="1" android:text="📺 Show Ad"
            android:textColor="#FFFFFF" android:backgroundTint="#2196F3" android:layout_marginStart="4dp"/>
    </LinearLayout>
</FrameLayout>
"""


def make_layout_startapp():
    return """<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent" android:layout_height="match_parent"
    android:orientation="vertical">
    <WebView android:id="@+id/webView"
        android:layout_width="match_parent" android:layout_height="0dp" android:layout_weight="1"/>
    <FrameLayout android:id="@+id/bannerLayout"
        android:layout_width="match_parent" android:layout_height="wrap_content"/>
    <LinearLayout android:layout_width="match_parent" android:layout_height="wrap_content"
        android:orientation="horizontal" android:padding="8dp" android:background="#CC000000">
        <Button android:id="@+id/btnRewarded"
            android:layout_width="0dp" android:layout_height="wrap_content"
            android:layout_weight="1" android:text="🎁 Watch Ad"
            android:textColor="#FFFFFF" android:backgroundTint="#4CAF50" android:layout_marginEnd="4dp"/>
        <Button android:id="@+id/btnInterstitial"
            android:layout_width="0dp" android:layout_height="wrap_content"
            android:layout_weight="1" android:text="📺 Show Ad"
            android:textColor="#FFFFFF" android:backgroundTint="#FF6B35" android:layout_marginStart="4dp"/>
    </LinearLayout>
</LinearLayout>
"""


def make_strings(app_name):
    return f'<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <string name="app_name">{app_name}</string>\n</resources>\n'


def make_root_gradle():
    return """buildscript {
    repositories { google(); mavenCentral() }
    dependencies { classpath 'com.android.tools.build:gradle:8.1.4' }
}
allprojects { repositories { google(); mavenCentral() } }
task clean(type: Delete) { delete rootProject.buildDir }
"""


def make_app_gradle_unity(pkg):
    return f"""plugins {{ id 'com.android.application' }}
android {{
    namespace '{pkg}'
    compileSdk 34
    defaultConfig {{ applicationId "{pkg}"; minSdk 21; targetSdk 34; versionCode 1; versionName "1.0" }}
    buildTypes {{ release {{ minifyEnabled false; proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro' }} }}
    compileOptions {{ sourceCompatibility JavaVersion.VERSION_1_8; targetCompatibility JavaVersion.VERSION_1_8 }}
}}
dependencies {{
    implementation 'androidx.appcompat:appcompat:1.6.1'
    implementation 'com.unity3d.ads:unity-ads:4.9.2'
}}
"""


def make_app_gradle_startapp(pkg):
    return f"""plugins {{ id 'com.android.application' }}
android {{
    namespace '{pkg}'
    compileSdk 34
    defaultConfig {{ applicationId "{pkg}"; minSdk 21; targetSdk 34; versionCode 1; versionName "1.0" }}
    buildTypes {{ release {{ minifyEnabled false; proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro' }} }}
    compileOptions {{ sourceCompatibility JavaVersion.VERSION_1_8; targetCompatibility JavaVersion.VERSION_1_8 }}
}}
dependencies {{
    implementation 'androidx.appcompat:appcompat:1.6.1'
    implementation 'com.startapp:inapp-sdk:4.10.4'
}}
"""


def make_gradle_properties():
    return "android.useAndroidX=true\nandroid.enableJetifier=true\norg.gradle.jvmargs=-Xmx2048m\n"


def make_settings(app_name):
    return f'rootProject.name = "{sanitize(app_name)}"\ninclude \':app\'\n'


def make_proguard(network="unity"):
    base = "-keep class com.unity3d.** { *; }\n-keep interface com.unity3d.** { *; }\n"
    if network == "startapp":
        base = "-keep class com.startapp.** { *; }\n-dontwarn com.startapp.**\n"
    return base


def make_workflow(app_name):
    safe = sanitize(app_name)
    return f"""name: Build APK
on:
  push:
    branches: [ main ]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up JDK 17
        uses: actions/setup-java@v4
        with:
          java-version: '17'
          distribution: 'temurin'
      - name: Setup Gradle
        uses: gradle/actions/setup-gradle@v4
        with:
          gradle-version: '8.4'
      - name: Build Debug APK
        run: gradle assembleDebug --no-daemon
      - name: Upload APK
        uses: actions/upload-artifact@v4
        with:
          name: {safe}-debug
          path: app/build/outputs/apk/debug/app-debug.apk
          retention-days: 7
"""


# ── GitHub: single atomic commit ─────────────────────────────────────────────

def commit_all_files(files, message):
    ref_resp, ref_status = gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/ref/heads/main")
    parents = [ref_resp["object"]["sha"]] if ref_status == 200 else []

    tree_body = {"tree": [{"path": p, "mode": "100644", "type": "blob", "content": c}
                           for p, c in files.items()]}
    tree_resp, ts = gh("POST", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/trees", tree_body)
    if ts not in (200, 201): return False, f"tree create failed: {tree_resp}"

    commit_body = {"message": message, "tree": tree_resp["sha"]}
    if parents: commit_body["parents"] = parents
    commit_resp, cs = gh("POST", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/commits", commit_body)
    if cs not in (200, 201): return False, f"commit create failed: {commit_resp}"
    new_sha = commit_resp["sha"]

    if parents:
        _, rs = gh("PATCH", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/refs/heads/main", {"sha": new_sha, "force": False})
    else:
        _, rs = gh("POST", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/refs", {"ref": "refs/heads/main", "sha": new_sha})
    if rs not in (200, 201): return False, f"ref update failed ({rs})"
    return True, new_sha


def ensure_repo_exists():
    _, status = gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}")
    if status == 404:
        gh("POST", "/user/repos", {"name": GITHUB_REPO, "private": False,
                                    "auto_init": False, "description": "APK Builder"})


def push_project(app_name, pkg, network, ad_id, content_type, content_value,
                 ad_types, auto_show, test_mode=False):
    ensure_repo_exists()
    pkg_path = pkg.replace(".", "/")

    has_rewarded     = "rewarded"     in ad_types
    has_interstitial = "interstitial" in ad_types
    has_banner       = "banner"       in ad_types

    if network == "unity":
        main_java  = make_main_activity_unity(pkg, ad_id, content_type, content_value,
                                              has_rewarded, has_interstitial, auto_show, test_mode)
        app_gradle = make_app_gradle_unity(pkg)
        layout     = make_layout_unity()
        layout_key = "app/src/main/res/layout/activity_main.xml"
    else:  # startapp
        main_java  = make_main_activity_startapp(pkg, ad_id, content_type, content_value,
                                                 has_banner, has_interstitial, has_rewarded, auto_show)
        app_gradle = make_app_gradle_startapp(pkg)
        layout     = make_layout_startapp()
        layout_key = "app/src/main/res/layout/activity_main_startapp.xml"

    files = {
        "build.gradle":                         make_root_gradle(),
        "settings.gradle":                      make_settings(app_name),
        "gradle.properties":                    make_gradle_properties(),
        "app/build.gradle":                     app_gradle,
        "app/proguard-rules.pro":               make_proguard(network),
        "app/src/main/AndroidManifest.xml":     make_manifest(pkg, app_name, network),
        layout_key:                              layout,
        "app/src/main/res/values/strings.xml":  make_strings(app_name),
        f"app/src/main/java/{pkg_path}/MainActivity.java": main_java,
        ".github/workflows/build.yml":          make_workflow(app_name),
    }
    return commit_all_files(files, f"Build: {app_name}")


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    host = request.host_url.rstrip("/")
    return render_template("index.html", github_owner=GITHUB_OWNER,
                           github_repo=GITHUB_REPO, host=host)


@app.route("/build", methods=["POST"])
def build():
    if not GITHUB_TOKEN or not GITHUB_OWNER:
        return jsonify({"success": False, "error": "Server config missing"}), 500

    d             = request.json or {}
    app_name      = d.get("app_name", "").strip()
    pkg           = d.get("package_name", "").strip()
    network       = d.get("network", "unity")          # "unity" | "startapp"
    ad_id         = d.get("ad_id", "").strip()         # Unity Game ID or StartApp App ID
    content_type  = d.get("content_type", "url")
    content_value = d.get("content_value", "").strip()
    ad_types      = d.get("ad_types", ["rewarded", "interstitial"])
    auto_show     = bool(d.get("auto_show", False))
    test_mode     = bool(d.get("test_mode", False))

    errors = []
    if not app_name:         errors.append("App name required")
    if not valid_pkg(pkg):   errors.append("Valid package name required (e.g. com.company.app)")
    if not ad_id:            errors.append("Ad Network ID required")
    if not content_value:    errors.append("URL ya HTML required")
    if content_type == "url" and content_value and not content_value.startswith(("http://", "https://")):
        errors.append("URL https:// se shuru karo")
    if not ad_types:         errors.append("Ek ad type select karo")
    if errors:
        return jsonify({"success": False, "error": " | ".join(errors)}), 400

    ok, result = push_project(app_name, pkg, network, ad_id,
                              content_type, content_value,
                              ad_types, auto_show, test_mode)
    if not ok:
        return jsonify({"success": False, "error": f"GitHub push failed: {result}"}), 500

    # Custom ad snippets for HTML content — same JS/URL bridge works for both networks now
    has_rewarded     = "rewarded"     in ad_types
    has_interstitial = "interstitial" in ad_types
    accent = "#4CAF50" if network == "unity" else "#4CAF50"
    r_btn = ('<a href="ads://show_rewarded" style="display:inline-block;padding:12px 20px;'
              f'background:{accent};color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;">'
              '🎁 Watch Ad for Reward</a>') if has_rewarded else ""
    i_btn = ('<a href="ads://show_interstitial" style="display:inline-block;padding:12px 20px;'
              'background:#2196F3;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;">'
              '📺 Show Ad</a>') if has_interstitial else ""
    js_r  = "window.NativeAds.showRewarded()"
    js_i  = "window.NativeAds.showInterstitial()"

    return jsonify({
        "success":      True,
        "commit_sha":   result,
        "actions_url":  f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/actions",
        "repo_url":     f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}",
        "message":      f"✅ '{app_name}' push ho gaya!",
        "snippets": {
            "rewarded_btn":     r_btn,
            "interstitial_btn": i_btn,
            "js_rewarded":      js_r,
            "js_interstitial":  js_i,
        }
    })


# ── Custom Ad API ────────────────────────────────────────────────────────────

@app.route("/api/generate", methods=["POST"])
def api_generate():
    d        = request.json or {}
    game_id  = d.get("game_id", "").strip()
    network  = d.get("network", "unity")
    ad_types = d.get("ad_types", ["rewarded", "interstitial"])
    if not game_id:
        return jsonify({"success": False, "error": "Game/App ID required"}), 400

    api_key = uuid.uuid4().hex[:24]
    AD_SESSIONS[api_key] = {"game_id": game_id, "network": network, "ad_types": ad_types}
    host = request.host_url.rstrip("/")
    endpoint = f"{host}/api/show/{api_key}"

    snippet = f"""<!-- Copy this into your HTML -->
<script>
function showAd(type) {{
  fetch('{endpoint}?type=' + (type||'rewarded'), {{method:'POST'}})
    .then(r=>r.json())
    .then(d=>{{
      if(d.success && window.NativeAds) {{
        if(type==='rewarded')     window.NativeAds.showRewarded();
        else                      window.NativeAds.showInterstitial();
      }}
    }});
}}
</script>

<!-- Paste onclick on ANY button in your HTML -->
<button onclick="showAd('rewarded')">🎁 Watch Ad for Reward</button>
<button onclick="showAd('interstitial')">📺 Show Ad</button>"""

    url_snippet = f"""<!-- OR use URL bridge — no JS needed! -->
<a href="ads://show_rewarded">🎁 Watch Ad for Reward</a>
<a href="ads://show_interstitial">📺 Show Ad</a>"""

    return jsonify({
        "success": True, "api_key": api_key,
        "endpoint": endpoint, "network": network,
        "game_id": game_id, "ad_types": ad_types,
        "js_snippet": snippet,
        "url_snippet": url_snippet,
        "test_html": f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ad Test</title>
<script>
function showAd(type){{fetch('{endpoint}?type='+type,{{method:'POST'}}).then(r=>r.json()).then(d=>{{if(window.NativeAds){{if(type==='rewarded')window.NativeAds.showRewarded();else window.NativeAds.showInterstitial();}}}});}}
</script>
<style>body{{font-family:sans-serif;text-align:center;padding:40px;background:#111;color:#fff}}
.btn{{display:inline-block;margin:10px;padding:14px 28px;border-radius:10px;font-size:16px;font-weight:bold;text-decoration:none;cursor:pointer;border:none;}}
</style></head><body>
<h2>Ad Test Page</h2>
<button class="btn" style="background:#4CAF50;color:#fff" onclick="showAd('rewarded')">🎁 Watch Rewarded Ad</button>
<button class="btn" style="background:#2196F3;color:#fff" onclick="showAd('interstitial')">📺 Show Interstitial</button>
<hr style="border-color:#333;margin:30px 0">
<p style="color:#888">URL Bridge (no JS needed):</p>
<a href="ads://show_rewarded" class="btn" style="background:#7c3aed;color:#fff">🎁 Rewarded (URL)</a>
<a href="ads://show_interstitial" class="btn" style="background:#0891b2;color:#fff">📺 Interstitial (URL)</a>
</body></html>"""
    })


@app.route("/api/show/<api_key>", methods=["POST", "GET", "OPTIONS"])
def api_show(api_key):
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"]  = "*"
        r.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return r
    s = AD_SESSIONS.get(api_key)
    if not s:
        return jsonify({"success": False, "error": "Invalid API key"}), 404
    ad_type = request.args.get("type", "rewarded")
    r = jsonify({"success": True, "ad_type": ad_type, "network": s["network"],
                 "game_id": s["game_id"], "message": f"{ad_type} ad trigger sent"})
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r


# ── Build status polling ──────────────────────────────────────────────────────

@app.route("/check-package")
def check_package():
    pkg = request.args.get("pkg", "").strip()
    return jsonify({"valid": valid_pkg(pkg)})


@app.route("/find-run/<sha>")
def find_run(sha):
    resp, status = gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs?head_sha={sha}")
    if status == 200 and resp.get("workflow_runs"):
        run = resp["workflow_runs"][0]
        return jsonify({"found": True, "run_id": run["id"],
                        "status": run["status"], "conclusion": run.get("conclusion"),
                        "html_url": run["html_url"]})
    return jsonify({"found": False})


@app.route("/run-status/<int:run_id>")
def run_status(run_id):
    resp, status = gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}")
    if status != 200: return jsonify({"error": "not found"}), 404
    return jsonify({"status": resp["status"], "conclusion": resp.get("conclusion"),
                    "html_url": resp["html_url"]})


@app.route("/download/<int:run_id>")
def download(run_id):
    resp, status = gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}/artifacts")
    if status != 200 or not resp.get("artifacts"):
        return "APK abhi nahi mila. Wait karo.", 404
    artifact = resp["artifacts"][0]
    req = urllib.request.Request(artifact["archive_download_url"])
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req) as r:
            zip_bytes = r.read()
    except urllib.error.HTTPError as e:
        return f"Download failed: {e.code}", 500
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    apk_entry = next((n for n in zf.namelist() if n.endswith(".apk")), None)
    if not apk_entry: return "APK file nahi mili", 500
    apk_bytes = zf.read(apk_entry)
    filename  = f"{sanitize(artifact['name'])}.apk"
    return Response(apk_bytes, mimetype="application/vnd.android.package-archive",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
