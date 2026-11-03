package com.salman.clinic;

import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.print.PrintAttributes;
import android.print.PrintDocumentAdapter;
import android.print.PrintManager;
import android.provider.MediaStore;
import android.util.Base64;
import android.webkit.WebView;

import androidx.core.content.FileProvider;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

@CapacitorPlugin(name = "AppNative")
public class AppNative extends Plugin {

    @PluginMethod
    public void print(final PluginCall call) {
        getActivity().runOnUiThread(new Runnable() {
            @Override
            public void run() {
                try {
                    WebView wv = getBridge().getWebView();
                    PrintManager pm = (PrintManager) getActivity().getSystemService(Context.PRINT_SERVICE);
                    PrintDocumentAdapter ad = wv.createPrintDocumentAdapter("Nuskha");
                    pm.print("Nuskha", ad, new PrintAttributes.Builder().build());
                    call.resolve();
                } catch (Exception e) {
                    call.reject("print: " + e);
                }
            }
        });
    }

    @PluginMethod
    public void savePdf(PluginCall call) {
        try {
            String b64 = call.getString("data");
            String name = call.getString("name");
            if (name == null || name.length() == 0) name = "nuskha.pdf";
            byte[] data = Base64.decode(b64, Base64.DEFAULT);
            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues v = new ContentValues();
                v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
                v.put(MediaStore.MediaColumns.MIME_TYPE, "application/pdf");
                v.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                Uri u = getContext().getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
                OutputStream o = getContext().getContentResolver().openOutputStream(u);
                o.write(data);
                o.close();
            } else {
                File dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                dir.mkdirs();
                FileOutputStream o = new FileOutputStream(new File(dir, name));
                o.write(data);
                o.close();
            }
            JSObject r = new JSObject();
            r.put("ok", true);
            call.resolve(r);
        } catch (Exception e) {
            call.reject("savePdf: " + e);
        }
    }

    @PluginMethod
    public void share(PluginCall call) {
        try {
            String text = call.getString("text");
            String title = call.getString("title");
            String b64 = call.getString("data");
            String name = call.getString("name");
            Intent i = new Intent(Intent.ACTION_SEND);
            if (b64 != null && b64.length() > 0) {
                if (name == null || name.length() == 0) name = "nuskha.pdf";
                byte[] data = Base64.decode(b64, Base64.DEFAULT);
                File dir = new File(getContext().getCacheDir(), "share");
                dir.mkdirs();
                File f = new File(dir, name);
                FileOutputStream o = new FileOutputStream(f);
                o.write(data);
                o.close();
                Uri u = FileProvider.getUriForFile(getContext(), getContext().getPackageName() + ".fileprovider", f);
                i.setType("application/pdf");
                i.putExtra(Intent.EXTRA_STREAM, u);
                i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            } else {
                i.setType("text/plain");
            }
            if (text != null) i.putExtra(Intent.EXTRA_TEXT, text);
            Intent ch = Intent.createChooser(i, title);
            getActivity().startActivity(ch);
            call.resolve();
        } catch (Exception e) {
            call.reject("share: " + e);
        }
    }

    private static volatile int dlPct = 0;
    private static volatile String dlState = "idle";
    private static volatile String dlErr = "";

    @PluginMethod
    public void canInstall(PluginCall call) {
        boolean ok = true;
        if (Build.VERSION.SDK_INT >= 26) ok = getContext().getPackageManager().canRequestPackageInstalls();
        JSObject r = new JSObject();
        r.put("ok", ok);
        call.resolve(r);
    }

    @PluginMethod
    public void askInstall(PluginCall call) {
        try {
            Intent i = new Intent("android.settings.MANAGE_UNKNOWN_APP_SOURCES", Uri.parse("package:" + getContext().getPackageName()));
            getActivity().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject("askInstall: " + e);
        }
    }

    @PluginMethod
    public void startUpdate(final PluginCall call) {
        final String url = call.getString("url");
        if ("run".equals(dlState)) { call.resolve(); return; }
        dlPct = 0; dlState = "run"; dlErr = "";
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                    c.setInstanceFollowRedirects(true);
                    c.setConnectTimeout(20000);
                    c.setReadTimeout(30000);
                    int code = c.getResponseCode();
                    int hops = 0;
                    while ((code == 301 || code == 302 || code == 303 || code == 307 || code == 308) && hops < 6) {
                        String loc = c.getHeaderField("Location");
                        c.disconnect();
                        c = (HttpURLConnection) new URL(loc).openConnection();
                        c.setConnectTimeout(20000);
                        c.setReadTimeout(30000);
                        code = c.getResponseCode();
                        hops++;
                    }
                    if (code != 200) throw new Exception("HTTP " + code);
                    long total = c.getContentLengthLong();
                    File dir = new File(getContext().getCacheDir(), "share");
                    dir.mkdirs();
                    File f = new File(dir, "ClinicApp.apk");
                    InputStream in = c.getInputStream();
                    FileOutputStream o = new FileOutputStream(f);
                    byte[] buf = new byte[65536];
                    long got = 0;
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        o.write(buf, 0, n);
                        got += n;
                        if (total > 0) dlPct = (int) (got * 100 / total);
                    }
                    o.close();
                    in.close();
                    dlPct = 100;
                    dlState = "done";
                } catch (Exception e) {
                    dlErr = String.valueOf(e);
                    dlState = "err";
                }
            }
        }).start();
        call.resolve();
    }

    @PluginMethod
    public void updateStatus(PluginCall call) {
        JSObject r = new JSObject();
        r.put("state", dlState);
        r.put("pct", dlPct);
        r.put("err", dlErr);
        call.resolve(r);
    }

    @PluginMethod
    public void installApk(PluginCall call) {
        try {
            File f = new File(new File(getContext().getCacheDir(), "share"), "ClinicApp.apk");
            Uri u = FileProvider.getUriForFile(getContext(), getContext().getPackageName() + ".fileprovider", f);
            Intent i = new Intent(Intent.ACTION_VIEW);
            i.setDataAndType(u, "application/vnd.android.package-archive");
            i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            getActivity().startActivity(i);
            dlState = "idle";
            call.resolve();
        } catch (Exception e) {
            call.reject("installApk: " + e);
        }
    }
}
