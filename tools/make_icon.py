# -*- coding: utf-8 -*-
"""生成应用图标 media_organizer.ico"""
import os
from PIL import Image, ImageDraw

W = H = 256
img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角背景
def rrect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)

# 深色底
rrect(d, (0, 0, W, H), 52, (11, 18, 32, 255))
# 文件夹
rrect(d, (28, 84, 228, 176), 22, (250, 204, 21, 255))
# 文件夹前盖
d.polygon([(28, 112), (28, 96), (72, 96), (88, 112)], fill=(250, 204, 21, 255))
# 媒体色块: 图(绿) 视频(红)
rrect(d, (56, 128, 112, 184), 10, (34, 197, 94, 255))   # 图片 绿
rrect(d, (120, 128, 176, 184), 10, (239, 68, 68, 255))   # 视频 红
# 播放三角
d.polygon([(140, 140), (140, 172), (168, 156)], fill=(255, 255, 255, 255))
# 图片小太阳/山
d.ellipse((76, 142, 92, 158), fill=(255, 255, 255, 255))

img.save(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "media_organizer.ico"),
         sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon saved")
