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
}
