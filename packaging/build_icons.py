"""Build CNG's original vector mark and native icons (Pillow, no network).

The C-shaped trunk, junctions and water drop stay legible at taskbar sizes.
SVG and raster exports share the same paths; run this only when artwork changes.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets'
SIZE = 1024
TRUNK = [('M', 346, 150), ('L', 202, 150), ('C', 170, 150, 150, 172, 150, 204),
         ('L', 150, 308), ('C', 150, 340, 172, 362, 204, 362), ('L', 346, 362)]
BRANCH = [('M', 150, 256), ('L', 232, 256), ('C', 262, 256, 267, 278, 302, 278)]
DROP = [('M', 344, 206), ('C', 332, 229, 304, 255, 304, 281),
        ('C', 304, 334, 384, 334, 384, 281), ('C', 384, 255, 356, 229, 344, 206)]
GLINT = [('M', 321, 277), ('C', 317, 292, 325, 303, 337, 305)]


def path_text(commands):
    return ' '.join(str(value) for command in commands for value in command)


def svg():
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <title>CNG 城市排水管网生成</title>
  <desc>深蓝绿圆角底上的 C 形管网、连接节点与青色水滴。</desc>
  <defs>
    <linearGradient id="ground" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="512" y2="512">
      <stop stop-color="#12364f"/><stop offset="1" stop-color="#087f83"/>
    </linearGradient>
    <linearGradient id="water" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="512" y2="512">
      <stop stop-color="#9af5e5"/><stop offset="1" stop-color="#38cbd1"/>
    </linearGradient>
  </defs>
  <rect x="24" y="24" width="464" height="464" rx="108" fill="url(#ground)"/>
  <rect x="25" y="25" width="462" height="462" rx="107" fill="none" stroke="#ffffff" stroke-opacity=".13" stroke-width="2"/>
  <path d="M88 204H424 M88 308H424 M202 88V424 M306 88V424" fill="none" stroke="#91e6df" stroke-opacity=".09" stroke-width="2"/>
  <g fill="none" stroke="#f5fcff" stroke-linecap="round" stroke-linejoin="round">
    <path d="{path_text(TRUNK)}" stroke-width="36"/>
    <path d="{path_text(BRANCH)}" stroke-width="24"/>
  </g>
  <g fill="#f5fcff">
    <circle cx="150" cy="256" r="25"/>
    <circle cx="346" cy="150" r="25"/>
    <circle cx="346" cy="362" r="25"/>
  </g>
  <circle cx="150" cy="256" r="10" fill="#126574"/>
  <g fill="#66e4dc"><circle cx="346" cy="150" r="10"/><circle cx="346" cy="362" r="10"/></g>
  <path d="{path_text(DROP)}Z" fill="url(#water)"/>
  <path d="{path_text(GLINT)}" fill="none" stroke="#e7fffa" stroke-width="7" stroke-linecap="round"/>
</svg>
'''


def points(commands):
    result = []
    for kind, *values in commands:
        if kind in ('M', 'L'):
            result.append(tuple(values))
        elif kind == 'C':
            x0, y0 = result[-1]
            x1, y1, x2, y2, x3, y3 = values
            for i in range(1, 65):
                t = i / 64
                q = 1 - t
                result.append((q**3*x0+3*q*q*t*x1+3*q*t*t*x2+t**3*x3,
                               q**3*y0+3*q*q*t*y1+3*q*t*t*y2+t**3*y3))
    return [(round(x * SIZE / 512), round(y * SIZE / 512)) for x, y in result]


def gradient(start, end):
    pixels = bytearray(SIZE * SIZE * 4)
    colors = [tuple(bytes.fromhex(color)) for color in (start, end)]
    for y in range(SIZE):
        for x in range(SIZE):
            t = (x+y) / (2*(SIZE-1))
            at = 4*(y*SIZE+x)
            pixels[at:at+4] = bytes([round(a+(b-a)*t) for a,b in zip(*colors)]+[255])
    return Image.frombytes('RGBA', (SIZE, SIZE), bytes(pixels))


def raster():
    scale = SIZE / 512
    image = Image.new('RGBA', (SIZE, SIZE))
    mask = Image.new('L', image.size)
    ImageDraw.Draw(mask).rounded_rectangle([24*scale,24*scale,488*scale,488*scale],radius=108*scale,fill=255)
    image.paste(gradient('12364f','087f83'), (0,0), mask)
    overlay = Image.new('RGBA', image.size)
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle([25*scale,25*scale,487*scale,487*scale],radius=107*scale,
                           outline=(255,255,255,33),width=round(2*scale))
    for line in [(88,204,424,204),(88,308,424,308),(202,88,202,424),(306,88,306,424)]:
        draw.line([round(v*scale) for v in line],fill=(145,230,223,23),width=round(2*scale))
    image.alpha_composite(overlay)
    draw = ImageDraw.Draw(image)

    def line(commands, width, color):
        pts = points(commands)
        draw.line(pts,fill=color,width=round(width*scale),joint='curve')
        radius = width*scale/2
        for x,y in (pts[0],pts[-1]):
            draw.ellipse([x-radius,y-radius,x+radius,y+radius],fill=color)

    def circle(x,y,r,color):
        draw.ellipse([(x-r)*scale,(y-r)*scale,(x+r)*scale,(y+r)*scale],fill=color)

    line(TRUNK,36,'#f5fcff')
    line(BRANCH,24,'#f5fcff')
    for x,y in [(150,256),(346,150),(346,362)]:
        circle(x,y,25,'#f5fcff')
        circle(x,y,10,'#126574' if x == 150 else '#66e4dc')
    drop_mask = Image.new('L',image.size)
    ImageDraw.Draw(drop_mask).polygon(points(DROP),fill=255)
    image.paste(gradient('9af5e5','38cbd1'),(0,0),drop_mask)
    line(GLINT,7,'#e7fffa')
    return image


def main():
    ASSETS.mkdir(exist_ok=True)
    vector = svg()
    (ASSETS/'app-icon.svg').write_text(vector,encoding='utf-8')
    (ROOT/'web'/'favicon.svg').write_text(vector,encoding='utf-8')
    icon = raster()
    icon.save(ASSETS/'app-icon.png')
    icon.save(ASSETS/'app-icon.ico',sizes=[(s,s) for s in (16,20,24,32,40,48,64,96,128,256)])
    icon.save(ASSETS/'app-icon.icns')
    (ROOT/'web'/'favicon.ico').write_bytes((ASSETS/'app-icon.ico').read_bytes())
    preview = Image.new('RGB',(1000,560),'#f4f7f8')
    draw = ImageDraw.Draw(preview)
    draw.rectangle((500,0,1000,560),fill='#122632')
    font = ImageFont.load_default(size=16)
    for x, label, color in [(0,'LIGHT','#375363'),(500,'DARK','#9ac0c9')]:
        draw.text((x+40,28),f'CNG  /  {label}',font=font,fill=color)
        large = icon.resize((300,300),Image.Resampling.LANCZOS)
        preview.paste(large,(x+100,65),large)
        left = x+46
        for size in (16,24,32,48,64,96):
            small = icon.resize((size,size),Image.Resampling.LANCZOS)
            preview.paste(small,(left,400+(96-size)//2),small)
            draw.text((left,510),str(size),font=font,fill=color)
            left += size+20
    output = ROOT/'.cache'/'icon-preview.png'
    output.parent.mkdir(exist_ok=True)
    preview.save(output)
    print('Created SVG, PNG, ICO, ICNS, favicons and light/dark preview.')


if __name__ == '__main__':
    main()
