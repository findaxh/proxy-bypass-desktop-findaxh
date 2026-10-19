# -*- coding: utf-8 -*-
"""生成 icon.ico(程序图标)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proxy_bypass import create_icon_image


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sizes = [16, 24, 32, 48, 64, 128, 256]
    img = create_icon_image(256)
    out = os.path.join(here, "icon.ico")
    img.save(out, sizes=[(s, s) for s in sizes])
    print("saved:", out)


if __name__ == "__main__":
    main()
