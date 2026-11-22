package com.salman.clinic;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;

/** PackageInstaller کے نتیجے کا وصول کنندہ: اگر سسٹم تصدیق مانگے تو وہی اسکرین کھول دیتا ہے۔ */
public class InstallResultReceiver extends BroadcastReceiver {
    @Override
    @SuppressWarnings("deprecation")
    public void onReceive(Context context, Intent intent) {
        try {
            int st = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -1);
            if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) {
                Intent c = (Intent) intent.getParcelableExtra(Intent.EXTRA_INTENT);
                if (c != null) {
                    c.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    context.startActivity(c);
                }
            }
        } catch (Throwable e) {
        }
    }
}
