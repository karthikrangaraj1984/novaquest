"""Draw picture-lesson diagrams as inline SVG/HTML (no image files, no scripts needed to read them).

Kinds: scene (an original labelled drawing with numbered markers and a legend), flow, cycle, compare, groups,
bars, layers, segments, equation and icons. Every picture carries a text alternative, and every label is
also real text in the legend, so screen readers and search get the full content."""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from html import escape
from typing import Any

from markupsafe import Markup

from novaquest.services.pictures import SCENES


def e(s: Any) -> str:
    return escape(str(s), quote=True)


@lru_cache(maxsize=64)
def _scene_svg(name: str) -> tuple[str, str]:
    """(viewBox, inner SVG) of a drawn scene."""
    raw = (SCENES / f"{name}.svg").read_text(encoding="utf-8")
    vb = re.search(r'viewBox="([^"]+)"', raw).group(1)
    inner = re.sub(r"^.*?<svg[^>]*>", "", raw, count=1, flags=re.S)
    inner = re.sub(r"</svg>\s*$", "", inner.strip())
    return vb, inner


def _anchors(name: str) -> dict[str, tuple[float, float]]:
    from novaquest.services.pictures import scene_anchors

    return scene_anchors()[name]


def scene(pic: dict, uid: str) -> str:
    vb, inner = _scene_svg(pic["scene"])
    pts = _anchors(pic["scene"])
    marks, legend = [], []
    if "ask" in pic:
        x, y = pts[pic["ask"]]
        marks.append(f'<g class="mk ask"><circle cx="{x}" cy="{y}" r="13"/><text x="{x}" y="{y}">?</text></g>')
    for i, lab in enumerate(pic.get("labels", []), 1):
        x, y = pts[lab["at"]]
        marks.append(f'<g class="mk" data-mark="{uid}-{i}"><circle cx="{x}" cy="{y}" r="11"/><text x="{x}" y="{y}">{i}</text></g>')
        note = f'<span class="nt"> – {e(lab["note"])}</span>' if lab.get("note") else ""
        legend.append(f'<li><button type="button" class="lg" data-mark="{uid}-{i}"><span class="num" aria-hidden="true">{i}</span>'
                      f'<span class="txt"><span class="term">{e(lab["name"])}</span>{note}</span></button></li>')
    alt = pic.get("alt") or "Labelled drawing: " + ", ".join(l["name"] for l in pic.get("labels", []))
    svg = (f'<svg class="scene-svg" viewBox="{vb}" role="img" aria-label="{e(alt)}" focusable="false">{inner}'
           f'{"".join(marks)}</svg>')
    if not legend:
        return f'<figure class="pic scene">{svg}</figure>'
    return (f'<figure class="pic scene" data-scene>{svg}<ol class="legend">{"".join(legend)}</ol>'
            f'<button type="button" class="btn small quiet hide-names" data-hide-names aria-pressed="false">'
            f'Test yourself: hide the names</button></figure>')


def flow(pic: dict, uid: str) -> str:
    steps = "".join(f'<li><span class="ic" aria-hidden="true">{e(s["icon"])}</span><span>{e(s["text"])}</span></li>'
                    for s in pic["steps"])
    return f'<figure class="pic flow"><ol>{steps}</ol></figure>'


def cycle(pic: dict, uid: str) -> str:
    n = len(pic["steps"])
    cx = cy = 180
    r = 118
    nodes, arrows = [], []
    pos = [(cx + r * math.sin(2 * math.pi * i / n), cy - r * math.cos(2 * math.pi * i / n)) for i in range(n)]
    for i, (x, y) in enumerate(pos):
        s = pic["steps"][i]
        nodes.append(f'<g class="node"><circle cx="{x:.1f}" cy="{y:.1f}" r="34"/>'
                     f'<text class="emo" x="{x:.1f}" y="{y - 2:.1f}">{e(s["icon"])}</text>'
                     f'<text class="lab" x="{x:.1f}" y="{y + 50:.1f}">{e(s["short"])}</text>'
                     f'<text class="no" x="{x - 26:.1f}" y="{y - 24:.1f}">{i + 1}</text></g>')
    for i in range(n):
        a0 = 2 * math.pi * i / n + 0.42
        a1 = 2 * math.pi * (i + 1) / n - 0.42
        x0, y0 = cx + r * math.sin(a0), cy - r * math.cos(a0)
        x1, y1 = cx + r * math.sin(a1), cy - r * math.cos(a1)
        arrows.append(f'<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 0 1 {x1:.1f},{y1:.1f}" marker-end="url(#ah-{uid})"/>')
    centre = f'<text class="centre" x="{cx}" y="{cy}">{e(pic.get("centre", ""))}</text>' if pic.get("centre") else ""
    alt = pic.get("alt") or "Cycle: " + " → ".join(s["short"] for s in pic["steps"]) + " → back to the start"
    svg = (f'<svg class="cycle-svg" viewBox="0 0 360 380" role="img" aria-label="{e(alt)}" focusable="false">'
           f'<defs><marker id="ah-{uid}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
           f'<path d="M0,0 L10,5 L0,10 z" class="ah"/></marker></defs><g class="arrows">{"".join(arrows)}</g>{centre}{"".join(nodes)}</svg>')
    steps = "".join(f'<li><b>{e(s["short"])}:</b> {e(s["text"])}</li>' for s in pic["steps"])
    return f'<figure class="pic cycle">{svg}<ol class="cycle-steps">{steps}</ol></figure>'


def compare(pic: dict, uid: str) -> str:
    cols, rows = pic["cols"], pic.get("rows")
    head = "".join(f'<th scope="col"><span class="ic" aria-hidden="true">{e(c["icon"])}</span>{e(c["head"])}</th>' for c in cols)
    if rows:
        body = "".join("<tr>" + f'<th scope="row">{e(row)}</th>' + "".join(f"<td>{e(c['cells'][i])}</td>" for c in cols) + "</tr>"
                       for i, row in enumerate(rows))
        head = '<th scope="col"><span class="sr-only">Feature</span></th>' + head
    else:
        n = max(len(c["cells"]) for c in cols)
        body = "".join("<tr>" + "".join(f"<td>{e(c['cells'][i]) if i < len(c['cells']) else ''}</td>" for c in cols) + "</tr>"
                       for i in range(n))
    return f'<figure class="pic compare cols-{len(cols)}"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></figure>'


def groups(pic: dict, uid: str) -> str:
    items = "".join(f'<li><span class="ic" aria-hidden="true">{e(i["icon"])}</span><b>{e(i["name"])}</b>'
                    + (f'<span class="small muted">{e(i["eg"])}</span>' if i.get("eg") else "") + "</li>" for i in pic["items"])
    return f'<figure class="pic groups"><div class="root">{e(pic["root"])}</div><ul>{items}</ul></figure>'


def bars(pic: dict, uid: str) -> str:
    top = max(i["value"] for i in pic["items"]) or 1
    unit = pic.get("unit", "")
    rows = []
    for i in pic["items"]:
        shown = i.get("show") or f'{i["value"]:g} {unit}'.strip()
        width = max(2.0, 100 * i["value"] / top)
        rows.append(f'<li><span class="lab">{e(i.get("icon", ""))} {e(i["label"])}</span>'
                    f'<span class="track"><span class="fill c-{e(i.get("color", "blue"))}" style="width:{width:.1f}%"></span></span>'
                    f'<span class="val">{e(shown)}</span></li>')
    return f'<figure class="pic bars"><ul>{"".join(rows)}</ul></figure>'


def layers(pic: dict, uid: str) -> str:
    rows = "".join(f'<li class="c-{e(i.get("color", "grey"))}"><b>{e(i["name"])}</b>'
                   + (f'<span>{e(i["note"])}</span>' if i.get("note") else "") + "</li>" for i in pic["items"])
    return f'<figure class="pic layers"><ol>{rows}</ol></figure>'


def segments(pic: dict, uid: str) -> str:
    segs = "".join(f'<li class="c-{e(i.get("color", "grey"))}"><b>{e(i["name"])}</b>'
                   + (f'<span>{e(i["note"])}</span>' if i.get("note") else "") + "</li>" for i in pic["items"])
    ticks = ""
    if pic.get("ticks"):
        ticks = '<div class="ticks" aria-hidden="true">' + "".join(f"<span>{e(t)}</span>" for t in pic["ticks"]) + "</div>"
    many = " many" if len(pic["items"]) > 4 else ""
    return f'<figure class="pic segments{many}"><ol>{segs}</ol>{ticks if not many else ""}</figure>'


def equation(pic: dict, uid: str) -> str:
    parts = []
    for p in pic["parts"]:
        if isinstance(p, str):
            parts.append(f'<span class="op" aria-label="{ {"+": "plus", "→": "gives", "=": "equals", "÷": "divided by", "×": "times", "⇌": "changes to and from"}[p] }">{e(p)}</span>')
        else:
            parts.append(f'<span class="term c-{e(p.get("color", "blue"))}"><span class="ic" aria-hidden="true">{e(p.get("icon", ""))}</span>{e(p["text"])}</span>')
    return f'<figure class="pic equation">{"".join(parts)}</figure>'


def icons(pic: dict, uid: str) -> str:
    tiles = "".join(f'<li><span class="ic" aria-hidden="true">{e(i["icon"])}</span><b>{e(i["text"])}</b>'
                    + (f'<span class="small muted">{e(i["sub"])}</span>' if i.get("sub") else "") + "</li>" for i in pic["items"])
    return f'<figure class="pic icons"><ul>{tiles}</ul></figure>'


_KINDS = {"scene": scene, "flow": flow, "cycle": cycle, "compare": compare, "groups": groups, "bars": bars,
          "layers": layers, "segments": segments, "equation": equation, "icons": icons}


@lru_cache(maxsize=2048)
def _render(spec: str, uid: str) -> str:
    pic = json.loads(spec)
    return _KINDS[pic["kind"]](pic, uid)


def picture(pic: dict, uid: str = "p") -> Markup:
    """Jinja global: draw one picture spec. `uid` keeps marker ids unique on the page."""
    return Markup(_render(json.dumps(pic, sort_keys=True, ensure_ascii=False), str(uid)))
