# icon-source.png (ریپو کی جڑ میں، 1024px) سے تمام آئیکن، اڈاپٹیو آئیکن اور اسپلیش بناتا ہے
import os
from PIL import Image
im = Image.open('icon-source.png').convert('RGBA')
bb = im.getchannel('A').getbbox() or (0, 0, im.width, im.height)
im = im.crop(bb); w = max(im.size)
sq = Image.new('RGBA', (w, w), (0, 0, 0, 0)); sq.paste(im, ((w-im.width)//2, (w-im.height)//2))
R = lambda n: sq.resize((n, n), Image.LANCZOS)
out = {}
out['icon-192.png'] = R(192); out['icon-512.png'] = R(512)
m = Image.new('RGBA', (512, 512), (255, 255, 255, 255)); c = R(400); m.paste(c, (56, 56), c); out['icon-maskable-512.png'] = m
a = Image.new('RGBA', (180, 180), (255, 255, 255, 255)); c = R(160); a.paste(c, (10, 10), c); out['apple-touch-icon.png'] = a.convert('RGB')
for n, im2 in out.items():
    im2.save(n); os.makedirs('pc', exist_ok=True); im2.save('pc/' + n)
R(256).save('pc/icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
res = 'ci/res'
for d, s in (('mdpi',48),('hdpi',72),('xhdpi',96),('xxhdpi',144),('xxxhdpi',192)):
    p = res + '/mipmap-' + d; os.makedirs(p, exist_ok=True)
    for nm in ('ic_launcher.png', 'ic_launcher_round.png'): R(s).save(p + '/' + nm)
# اڈاپٹیو آئیکن (اینڈرائیڈ 8+ اور اسپلیش اسکرین): 108dp کینوس، بیج درمیان میں 62%
for d, s in (('mdpi',108),('hdpi',162),('xhdpi',216),('xxhdpi',324),('xxxhdpi',432)):
    p = res + '/mipmap-' + d; cv = Image.new('RGBA', (s, s), (0, 0, 0, 0)); k = int(s * 0.62); b = R(k); cv.paste(b, ((s-k)//2, (s-k)//2), b); cv.save(p + '/clinic_fg.png')
os.makedirs(res + '/mipmap-anydpi-v26', exist_ok=True)
for nm in ('ic_launcher.xml', 'ic_launcher_round.xml'):
    open(res + '/mipmap-anydpi-v26/' + nm, 'w').write('<?xml version="1.0" encoding="utf-8"?>\n<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n    <background android:drawable="@color/clinic_icon_bg"/>\n    <foreground android:drawable="@mipmap/clinic_fg"/>\n</adaptive-icon>\n')
os.makedirs(res + '/values', exist_ok=True)
open(res + '/values/clinic_icon.xml', 'w').write('<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <color name="clinic_icon_bg">#FFFFFF</color>\n</resources>\n')
# پرانے اینڈرائیڈ کی اسپلیش: سفید پس منظر + درمیان میں بڑا صاف آئیکن
os.makedirs(res + '/drawable-nodpi', exist_ok=True); R(768).save(res + '/drawable-nodpi/clinic_splash_icon.png')
os.makedirs(res + '/drawable', exist_ok=True)
open(res + '/drawable/splash.xml', 'w').write('<?xml version="1.0" encoding="utf-8"?>\n<layer-list xmlns:android="http://schemas.android.com/apk/res/android">\n    <item android:drawable="@color/clinic_icon_bg"/>\n    <item android:gravity="center" android:width="240dp" android:height="240dp">\n        <bitmap android:src="@drawable/clinic_splash_icon" android:gravity="fill"/>\n    </item>\n</layer-list>\n')
print('icons ok')
