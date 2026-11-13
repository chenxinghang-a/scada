"""把全量点击的截图拼成接触表（contact sheet），一页一张。

为什么需要：单独看 200+ 张 1672×1050 的截图不现实，
但"只给统计数字"又等于没验收 —— 得能一眼看出"点完这页变成了什么"。

每张缩略图左上角标序号 + 按钮文本，方便和 JSON 里的记录对上。
"""

from __future__ import annotations

import glob
import io as _io
import os
import sys

from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join('build', 'e2e', 'all_buttons')
SHOTS = os.path.join(OUT, 'shots')

COLS = 4
THUMB_W = 420
LABEL_H = 22
PAD = 6


def _font(size=14):
    for name in ('msyh.ttc', 'simhei.ttf', 'arial.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def main(argv=None):
    pages = sys.argv[1:] or sorted({os.path.basename(p).split('_')[0]
                                    for p in glob.glob(os.path.join(SHOTS, '*.png'))})
    made = []
    for page in pages:
        files = sorted(glob.glob(os.path.join(SHOTS, f'{page}_*.png')))
        if not files:
            continue
        imgs = []
        for f in files:
            try:
                im = Image.open(f).convert('RGB')
            except Exception:
                continue
            h = int(THUMB_W * im.height / im.width)
            imgs.append((os.path.basename(f), im.resize((THUMB_W, h), Image.LANCZOS), h))

        if not imgs:
            continue
        th = max(h for _, _, h in imgs)
        rows = (len(imgs) + COLS - 1) // COLS
        W = COLS * THUMB_W + (COLS + 1) * PAD
        H = rows * (th + LABEL_H + PAD) + PAD
        sheet = Image.new('RGB', (W, H), (245, 246, 248))
        draw = ImageDraw.Draw(sheet)
        f = _font(13)

        for i, (name, im, h) in enumerate(imgs):
            r, c = divmod(i, COLS)
            x = PAD + c * (THUMB_W + PAD)
            y = PAD + r * (th + LABEL_H + PAD)
            sheet.paste(im, (x, y + LABEL_H))
            label = name.replace('.png', '')[:52]
            draw.text((x + 2, y + 3), label, fill=(30, 34, 40), font=f)
            draw.rectangle([x - 1, y - 1, x + THUMB_W, y + LABEL_H + h],
                           outline=(200, 205, 212), width=1)

        out = os.path.join(OUT, f'contact_{page}.png')
        sheet.save(out, optimize=True)
        made.append((out, len(imgs), sheet.size))
        print(f'{page:14} {len(imgs):3} 张 -> {out}  {sheet.size}')

    if not made:
        print('没有截图可拼')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
