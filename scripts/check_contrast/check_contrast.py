"""Check the actual design token text and control-border contrast pairs."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
tokens = json.loads((root / "design/tokens.json").read_text(encoding="utf-8"))["colors"]


def luminance(color):
    channels = [int(color[i:i+2], 16)/255 for i in (1,3,5)]
    return sum(weight*(channel/12.92 if channel <= .04045 else ((channel+.055)/1.055)**2.4)
               for channel,weight in zip(channels,(.2126,.7152,.0722)))


pairs = []
for foreground in ("text","muted","purple","gold","error"):
    for background in ("base","surface"):
        pairs.append((foreground,background,4.5))
pairs.extend(("control_border",background,3) for background in ("base","surface"))
results = []
for fg,bg,minimum in pairs:
    a,b = sorted((luminance(tokens[fg]),luminance(tokens[bg])))
    ratio = (b+.05)/(a+.05)
    assert ratio >= minimum, f"{fg}/{bg} contrast {ratio} below {minimum}"
    results.append({"foreground":fg,"background":bg,"ratio":round(ratio,3),"minimum":minimum})
(root / "reports/contrast.json").write_text(json.dumps({"status":"passed","pairs":results},indent=2),encoding="utf-8")
print(f"Passed {len(results)} WCAG AA text and control contrast checks")
