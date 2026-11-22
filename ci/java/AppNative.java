package com.salman.clinic;

import android.Manifest;
import android.app.DownloadManager;
import android.content.pm.PackageManager;
import android.telephony.SmsManager;
import java.util.ArrayList;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import android.content.ContentValues;
import android.database.Cursor;
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
    public void smsPerm(PluginCall call) {
        boolean ok = ContextCompat.checkSelfPermission(getContext(), android.Manifest.permission.SEND_SMS) == android.content.pm.PackageManager.PERMISSION_GRANTED;
        if (!ok) ActivityCompat.requestPermissions(getActivity(), new String[]{android.Manifest.permission.SEND_SMS}, 9001);
        JSObject r = new JSObject();
        r.put("ok", ok);
        call.resolve(r);
    }

    @PluginMethod
    public void sendSms(PluginCall call) {
        try {
            if (ContextCompat.checkSelfPermission(getContext(), android.Manifest.permission.SEND_SMS) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                call.reject("no permission");
                return;
            }
            String phone = call.getString("phone");
            String text = call.getString("text");
            SmsManager sm = Build.VERSION.SDK_INT >= 31 ? getContext().getSystemService(SmsManager.class) : SmsManager.getDefault();
            ArrayList<String> parts = sm.divideMessage(text);
            sm.sendMultipartTextMessage(phone, null, parts, null, null);
            JSObject r = new JSObject();
            r.put("ok", true);
            call.resolve(r);
        } catch (Exception e) {
            call.reject(String.valueOf(e.getMessage()));
        }
    }

    @PluginMethod
    public void exit(PluginCall call) {
        call.resolve();
        getActivity().finish();
    }

    @PluginMethod
    public void print(final PluginCall call) {
        getActivity().runOnUiThread(new Runnable() {
            @Override
            public void run() {
                try {
                    WebView wv = getBridge().getWebView();
                    PrintManager pm = (PrintManager) getActivity().getSystemService(Context.PRINT_SERVICE);
                    PrintDocumentAdapter ad = wv.createPrintDocumentAdapter("Nuskha");
                    pm.print("Nuskha", ad, new PrintAttributes.Builder().setMediaSize(PrintAttributes.MediaSize.ISO_A4).setMinMargins(PrintAttributes.Margins.NO_MARGINS).setColorMode(PrintAttributes.COLOR_MODE_MONOCHROME).build());
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
            String mime = call.getString("mime");
            if (mime == null || mime.length() == 0) mime = "application/pdf";
            byte[] data = Base64.decode(b64, Base64.DEFAULT);
            String savedUri = null;
            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues v = new ContentValues();
                v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
                v.put(MediaStore.MediaColumns.MIME_TYPE, mime);
                v.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                Uri u = getContext().getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
                OutputStream o = getContext().getContentResolver().openOutputStream(u);
                o.write(data);
                o.close();
                savedUri = u.toString();
            } else {
                File dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                dir.mkdirs();
                File pf = new File(dir, name);
                FileOutputStream o = new FileOutputStream(pf);
                o.write(data);
                o.close();
                try {
                    savedUri = FileProvider.getUriForFile(getContext(), getContext().getPackageName() + ".fileprovider", pf).toString();
                } catch (Exception ex) { savedUri = null; }
            }
            JSObject r = new JSObject();
            r.put("ok", true);
            if (savedUri != null) r.put("uri", savedUri);
            call.resolve(r);
        } catch (Exception e) {
            call.reject("savePdf: " + e);
        }
    }

    @PluginMethod
    public void openPdf(PluginCall call) {
        try {
            Uri u = Uri.parse(call.getString("uri"));
            Intent i = new Intent(Intent.ACTION_VIEW);
            i.setDataAndType(u, "application/pdf");
            i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            getActivity().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject("openPdf: " + e);
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
            String phone = call.getString("phone");
            if (phone != null && phone.length() > 0 && b64 != null && b64.length() > 0) {
                String[] pks = {"com.whatsapp", "com.whatsapp.w4b"};
                for (String pk : pks) {
                    try {
                        Intent w = new Intent(i);
                        w.setPackage(pk);
                        w.putExtra("jid", phone + "@s.whatsapp.net");
                        getActivity().startActivity(w);
                        call.resolve();
                        return;
                    } catch (Exception ex) {
                    }
                }
            }
            Intent ch = Intent.createChooser(i, title);
            getActivity().startActivity(ch);
            call.resolve();
        } catch (Exception e) {
            call.reject("share: " + e);
        }
    }


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

    private long savedId() {
        return getContext().getSharedPreferences("upd", Context.MODE_PRIVATE).getLong("id", -1);
    }

    private void saveId(long id) {
        getContext().getSharedPreferences("upd", Context.MODE_PRIVATE).edit().putLong("id", id).apply();
    }

    private Cursor queryId(long id) {
        if (id < 0) return null;
        DownloadManager dm = (DownloadManager) getContext().getSystemService(Context.DOWNLOAD_SERVICE);
        Cursor c = dm.query(new DownloadManager.Query().setFilterById(id));
        if (c != null && c.moveToFirst()) return c;
        if (c != null) c.close();
        return null;
    }

    @PluginMethod
    public void startUpdate(PluginCall call) {
        try {
            String url = call.getString("url");
            DownloadManager dm = (DownloadManager) getContext().getSystemService(Context.DOWNLOAD_SERVICE);
            long old = savedId();
            Cursor c = queryId(old);
            if (c != null) {
                int st = c.getInt(c.getColumnIndex(DownloadManager.COLUMN_STATUS));
                c.close();
                if (st == DownloadManager.STATUS_RUNNING || st == DownloadManager.STATUS_PENDING || st == DownloadManager.STATUS_PAUSED) {
                    call.resolve();
                    return;
                }
                dm.remove(old);
            }
            DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
            r.setTitle("Clinic update");
            r.setDescription("ClinicApp.apk");
            r.setMimeType("application/vnd.android.package-archive");
            r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            r.setAllowedOverMetered(true);
            r.setAllowedOverRoaming(true);
            r.setDestinationInExternalFilesDir(getContext(), Environment.DIRECTORY_DOWNLOADS, "ClinicApp-update.apk");
            File f = new File(getContext().getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), "ClinicApp-update.apk");
            if (f.exists()) f.delete();
            saveId(dm.enqueue(r));
            call.resolve();
        } catch (Exception e) {
            call.reject("startUpdate: " + e);
        }
    }

    @PluginMethod
    public void updateStatus(PluginCall call) {
        JSObject r = new JSObject();
        r.put("state", "idle");
        r.put("pct", 0);
        try {
            Cursor c = queryId(savedId());
            if (c != null) {
                int st = c.getInt(c.getColumnIndex(DownloadManager.COLUMN_STATUS));
                long got = c.getLong(c.getColumnIndex(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR));
                long tot = c.getLong(c.getColumnIndex(DownloadManager.COLUMN_TOTAL_SIZE_BYTES));
                int reason = c.getInt(c.getColumnIndex(DownloadManager.COLUMN_REASON));
                c.close();
                int pct = tot > 0 ? (int) (got * 100 / tot) : 0;
                r.put("pct", pct);
                if (st == DownloadManager.STATUS_SUCCESSFUL) r.put("state", "done");
                else if (st == DownloadManager.STATUS_FAILED) r.put("state", "err");
                else if (st == DownloadManager.STATUS_PAUSED) {
                    r.put("state", "paused");
                    r.put("why", reason == DownloadManager.PAUSED_WAITING_FOR_NETWORK ? "نیٹ کا انتظار" : reason == DownloadManager.PAUSED_QUEUED_FOR_WIFI ? "وائی فائی کا انتظار" : "انتظار");
                } else r.put("state", "run");
            }
        } catch (Exception e) {
            r.put("state", "err");
        }
        call.resolve(r);
    }

    @PluginMethod
    public void installApk(PluginCall call) {
        try {
            getContext().getSharedPreferences("clinic_helper", Context.MODE_PRIVATE).edit().putLong("armed", System.currentTimeMillis()).apply();
            DownloadManager dm = (DownloadManager) getContext().getSystemService(Context.DOWNLOAD_SERVICE);
            Uri u = dm.getUriForDownloadedFile(savedId());
            if (u == null) throw new Exception("file missing");
            try {
                installSession(u);
                call.resolve();
                return;
            } catch (Throwable se) {
            }
            Intent i = new Intent(Intent.ACTION_VIEW);
            i.setDataAndType(u, "application/vnd.android.package-archive");
            i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            getActivity().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject("installApk: " + e);
        }
    }

    /** سیشن کے ذریعے انسٹال: اینڈرائیڈ 12+ پر اگلی اپ ڈیٹس بغیر تصدیق اسکرین کے ہو جاتی ہیں، اور ایکسیسبلٹی کی پابندی (Restricted setting) بھی ہٹ جاتی ہے۔ */
    private void installSession(Uri u) throws Exception {
        Context cx = getContext();
        android.content.pm.PackageInstaller pi = cx.getPackageManager().getPackageInstaller();
        android.content.pm.PackageInstaller.SessionParams sp = new android.content.pm.PackageInstaller.SessionParams(android.content.pm.PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        if (Build.VERSION.SDK_INT >= 31) sp.setRequireUserAction(android.content.pm.PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
        int sid = pi.createSession(sp);
        android.content.pm.PackageInstaller.Session ss = pi.openSession(sid);
        try {
            InputStream in = cx.getContentResolver().openInputStream(u);
            OutputStream out = ss.openWrite("clinic.apk", 0, -1);
            byte[] b = new byte[65536];
            int n;
            while ((n = in.read(b)) > 0) out.write(b, 0, n);
            ss.fsync(out);
            in.close();
            out.close();
            Intent it = new Intent(cx, InstallResultReceiver.class);
            it.setAction("com.salman.clinic.INSTALL_RESULT");
            int fl = android.app.PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? android.app.PendingIntent.FLAG_MUTABLE : 0);
            android.app.PendingIntent pend = android.app.PendingIntent.getBroadcast(cx, sid, it, fl);
            ss.commit(pend.getIntentSender());
        } catch (Exception e) {
            ss.abandon();
            throw e;
        } finally {
            ss.close();
        }
    }

    @PluginMethod
    public void helperStatus(PluginCall call) {
        JSObject r = new JSObject();
        boolean on = false;
        try {
            String v = android.provider.Settings.Secure.getString(getContext().getContentResolver(), android.provider.Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
            on = v != null && v.contains(getContext().getPackageName() + "/");
        } catch (Exception e) {
        }
        r.put("on", on);
        call.resolve(r);
    }

    @PluginMethod
    public void helperSettings(PluginCall call) {
        try {
            Intent i = new Intent(android.provider.Settings.ACTION_ACCESSIBILITY_SETTINGS);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject(String.valueOf(e));
        }
    }

    @PluginMethod
    public void helperArm(PluginCall call) {
        getContext().getSharedPreferences("clinic_helper", Context.MODE_PRIVATE).edit().putLong("armed", System.currentTimeMillis()).apply();
        call.resolve();
    }

    @PluginMethod
    public void waSend(final PluginCall call) {
        final Context cx = getContext();
        try {
            String phone = call.getString("phone", "").replaceAll("[^0-9]", "");
            String text = call.getString("text", "");
            if (phone.length() < 10) { call.reject("bad phone"); return; }
            final android.content.SharedPreferences sp = cx.getSharedPreferences("clinic_helper", Context.MODE_PRIVATE);
            sp.edit().putLong("wa_armed", System.currentTimeMillis()).putString("wa_res", "").apply();
            Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse("https://wa.me/" + phone + "?text=" + Uri.encode(text)));
            i.setPackage("com.whatsapp");
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try { cx.startActivity(i); } catch (Exception e1) {
                i.setPackage("com.whatsapp.w4b");
                try { cx.startActivity(i); } catch (Exception e2) { call.reject("no whatsapp"); return; }
            }
            new Thread(new Runnable() {
                public void run() {
                    String res = "";
                    for (int k = 0; k < 40 && res.isEmpty(); k++) {
                        try { Thread.sleep(500); } catch (Exception e) { }
                        res = sp.getString("wa_res", "");
                    }
                    try { Thread.sleep(1500); } catch (Exception e) { }
                    sp.edit().putLong("wa_armed", 0).apply();
                    try {
                        Intent b = cx.getPackageManager().getLaunchIntentForPackage(cx.getPackageName());
                        if (b != null) { b.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT); cx.startActivity(b); }
                    } catch (Exception e) { }
                    JSObject r = new JSObject();
                    r.put("ok", res.equals("ok"));
                    r.put("res", res.isEmpty() ? "timeout" : res);
                    call.resolve(r);
                }
            }).start();
        } catch (Exception e) {
            call.reject(String.valueOf(e));
        }
    }
}
