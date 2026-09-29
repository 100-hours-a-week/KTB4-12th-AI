"""HTML 표로 그린 그림을 PNG로 — mermaid로는 배치가 안 나오는 "칩 표" 그림용 (Chrome 헤드리스, 인터넷 불필요).
python3 build_roundtrips.py
  docs/assets/loadtest/04-왕복-전후.html → docs/assets/loadtest/04-왕복-전후.png   (7.6 한 건의 DB 왕복 — 수정 전 34 → 수정 후 8)

그림을 고치려면 HTML을 고치고 이 스크립트를 다시 돌린다 — 그림과 소스가 한 벌. 새 HTML 그림은 TARGETS 에 한 줄 넣는다.
PNG 여백은 PIL 이 있으면 자르고, 없으면 그대로 둔다(build_be_sequences.py 와 같은 규칙)."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TARGETS: list[tuple[Path, int, int]] = [
    # (HTML, 창 너비, 창 높이) — 너비는 HTML body 의 width 보다 조금 넓게, 높이는 넉넉히(여백은 _trim 이 자른다)
    (HERE / "loadtest" / "04-왕복-전후.html", 1510, 1000),
]


def main() -> int:
    for html, w, h in TARGETS:
        if not html.exists():
            print("없음", html, file=sys.stderr)
            return 1
        png = html.with_suffix(".png")
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--virtual-time-budget=5000",
                        "--force-device-scale-factor=2", f"--window-size={w},{h}", f"--screenshot={png}", f"file://{html}"],
                       check=True, capture_output=True)
        _trim(png)
        print("ok", png.relative_to(HERE))
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
        im.crop((max(0, x0 - 30), max(0, y0 - 30), min(im.width, x1 + 30), min(im.height, y1 + 30))).save(png)


if __name__ == "__main__":
    sys.exit(main())
