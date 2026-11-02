# icon-source.png (ریپو کی جڑ میں) سے تمام آئیکن بناتا ہے
import os, shutil
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
for d, s in (('mdpi',48),('hdpi',72),('xhdpi',96),('xxhdpi',144),('xxxhdpi',192)):
    p = 'ci/res/mipmap-' + d; os.makedirs(p, exist_ok=True)
    for nm in ('ic_launcher.png', 'ic_launcher_round.png'): R(s).save(p + '/' + nm)
print('icons ok')
