"""docs/BE_연동_필드표_v*.md 안의 mermaid 시퀀스 다이어그램을 PNG로 — `<!-- fig: 이름 -->` 표시가 붙은 ```mermaid 블록만.
python3 build_be_sequences.py  → docs/assets/be-seq/<v1|v2|v3>/<이름>.png   (Chrome 헤드리스 + mermaid CDN. 인터넷 필요)
문서의 mermaid를 고치면 이 스크립트를 다시 돌린다 — 그림과 본문이 한 소스."""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCS = sorted(HERE.parent.glob("BE_연동_필드표_v*.md"))
OUT = HERE / "be-seq"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HTML = """<!doctype html><html><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>body{{margin:0;padding:16px;background:#fff;font-family:"Apple SD Gothic Neo","Noto Sans KR",sans-serif}}</style></head>
<body><pre class="mermaid">{src}</pre>
<script>mermaid.initialize({{startOnLoad:true,theme:'neutral',sequence:{{actorFontSize:15,messageFontSize:13,noteFontSize:12,width:200,actorMargin:230,messageMargin:40,wrap:true,wrapPadding:12,useMaxWidth:false}}}});</script>
</body></html>"""


def main() -> int:
    if not DOCS:
        print("BE_연동_필드표_v*.md 없음", file=sys.stderr)
        return 1
    for doc in DOCS:
        ver = doc.stem.rsplit("_", 1)[-1]                      # v1 · v2 · v3
        out = OUT / ver
        out.mkdir(parents=True, exist_ok=True)
        text = doc.read_text(encoding="utf-8")
        figs = re.findall(r"<!-- fig: ([\w-]+) -->\s*```mermaid\n(.*?)```", text, re.S)
        for name, src in figs:
            html = out / f"{name}.html"
            html.write_text(HTML.format(src=src), encoding="utf-8")
            png = out / f"{name}.png"
            h = 400 + src.count("\n") * 120                      # 넉넉히 (여백은 아래서 자름)
            subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--virtual-time-budget=10000",
                            "--force-device-scale-factor=2", f"--window-size=2600,{h}", f"--screenshot={png}", f"file://{html}"],
                           check=True, capture_output=True)
            html.unlink()
            _trim(png)
            print("ok", ver, png.name)
    return 0


def _trim(png: Path) -> None:
    """흰 여백 자르기 (PIL 있을 때만)."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return
    im = Image.open(png).convert("RGB")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    box = ImageChops.difference(im, bg).getbbox()
    if box:
        x0, y0, x1, y1 = box
        im.crop((max(0, x0 - 24), max(0, y0 - 24), min(im.width, x1 + 24), min(im.height, y1 + 24))).save(png)


if __name__ == "__main__":
    sys.exit(main())
