#!/usr/bin/env python3
"""Build the launch gallery images and the site's og.png.

Every number comes from docs/field-results/2026-10-aeas-earthquakes.json.
Rendering uses a local Chrome/Chromium in headless mode:

    python3 docs/launch/build_gallery.py [--chrome PATH]

Writes docs/launch/gallery-*.png (1270x760) and docs/og.png (1200x630).
"""
import argparse
import html
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.dirname(HERE)
DATA = os.path.join(DOCS, "field-results", "2026-10-aeas-earthquakes.json")
LOGO = open(os.path.join(DOCS, "logo.svg"), encoding="utf-8").read()
LOGO = LOGO[LOGO.index("<svg"):].replace('width="512" height="512"', 'width="100%" height="100%"')

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "chromium", "chromium-browser",
]

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap');
* { box-sizing: border-box; margin: 0; padding: 0; }
:root { --bg:#030712; --panel:#0a0f1d; --line:rgba(148,163,184,.14); --line2:rgba(148,163,184,.24);
        --cyan:#38bdf8; --green:#22c55e; --amber:#eab308; --red:#ef4444; --text:#e6e9ef; --muted:#8a94a6; --dim:#5a6478; }
html, body { width: __W__px; height: __H__px; overflow: hidden; background: var(--bg); color: var(--text);
             font-family: Geist, -apple-system, 'Helvetica Neue', sans-serif; }
body { position: relative;
       background-image: radial-gradient(ellipse 70% 60% at 85% 0%, rgba(56,189,248,.10), transparent 60%),
                         linear-gradient(var(--line) 1px, transparent 1px), linear-gradient(90deg, var(--line) 1px, transparent 1px);
       background-size: auto, 56px 56px, 56px 56px; background-position: 0 0, -1px -1px, -1px -1px; }
body::after { content:""; position:absolute; inset:0; background: radial-gradient(ellipse 120% 90% at 30% 40%, transparent 40%, var(--bg) 100%); pointer-events:none; }
.mono { font-family: 'Geist Mono', ui-monospace, Menlo, monospace; }
.wrap { position: relative; z-index: 1; padding: 56px 64px; height: 100%; }
.brand { display:flex; align-items:center; gap:16px; font-weight:600; font-size:22px; }
.brand .logo { width:56px; height:56px; }
.kicker { font-family:'Geist Mono',monospace; font-size:15px; color:var(--cyan); letter-spacing:.04em; }
h1 { font-weight:700; letter-spacing:-.035em; line-height:1.02; }
.soft { color:#7c8799; }
.chip { display:inline-flex; gap:8px; align-items:center; border:1px solid var(--line2); border-radius:999px; padding:8px 16px;
        font-family:'Geist Mono',monospace; font-size:16px; color:var(--muted); background:rgba(10,15,29,.7); }
.chip b { color:var(--text); font-weight:500; }
.card { background:rgba(10,15,29,.88); border:1px solid var(--line2); border-radius:18px; }
.ok { color:var(--green); } .warn { color:var(--amber); } .bad { color:var(--red); } .cy { color:var(--cyan); }
"""


def page(w, h, body, extra_css=""):
    return ("<!doctype html><html><head><meta charset='utf-8'><style>" + CSS.replace("__W__", str(w)).replace("__H__", str(h))
            + extra_css + "</style></head><body>" + body + "</body></html>")


def wave(width, y0, amp, bursts, color, seed, opacity=1.0):
    """A seismogram-like SVG path: background noise plus decaying bursts at (x_fraction, strength)."""
    rnd = random.Random(seed)
    pts = []
    for x in range(0, width + 1, 3):
        v = rnd.uniform(-1, 1) * 0.06
        for bx, s in bursts:
            d = x - bx * width
            if d >= 0:
                v += s * math.exp(-d / (width * 0.07)) * math.sin(d / 5.5)
        pts.append(f"{x},{y0 + v * amp:.1f}")
    return (f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="2.4" '
            f'stroke-linejoin="round" opacity="{opacity}"/>')


def logo_block(size=56):
    return f'<div class="brand"><div class="logo" style="width:{size}px;height:{size}px">{LOGO}</div>Quake MCS Listener</div>'


def hero(w, h, gallery=True):
    waves = (f'<svg width="{w}" height="230" style="position:absolute;left:0;bottom:0;z-index:1">'
             + wave(w, 70, 70, [(0.36, 0.5), (0.68, 1.0)], "#38bdf8", 3, 0.95)
             + wave(w, 165, 80, [(0.40, 0.25), (0.76, 1.1)], "#22c55e", 7, 0.95) + "</svg>")
    notif = "" if not gallery else """
      <div class="card" style="position:absolute;right:64px;top:150px;width:420px;padding:22px 24px;z-index:2;box-shadow:0 20px 60px rgba(0,0,0,.5)">
        <div class="mono" style="font-size:13px;color:var(--dim);letter-spacing:.06em">WEBHOOK · LEVEL ALERT</div>
        <div style="font-size:30px;font-weight:700;margin-top:10px">🚨 Shaking in <span class="bad">18 s</span></div>
        <div class="mono" style="font-size:15px;color:var(--muted);margin-top:10px;line-height:1.6">M6.1 · 96 km away<br>est. intensity at your place: <b style="color:var(--text)">MMI 5.4</b></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px">
          <span class="chip" style="font-size:13px;padding:5px 11px">→ lights</span><span class="chip" style="font-size:13px;padding:5px 11px">→ siren</span>
          <span class="chip" style="font-size:13px;padding:5px 11px">→ your server</span></div>
      </div>"""
    title_size = 68 if gallery else 76
    body = f"""
    <div class="wrap">
      {logo_block()}
      <h1 style="font-size:{title_size}px;margin-top:{60 if gallery else 52}px;max-width:{700 if gallery else 1000}px">Earthquake warnings,<br><span class="soft">for anything you automate.</span></h1>
      <div style="display:flex;gap:12px;margin-top:34px;flex-wrap:wrap;max-width:{720 if gallery else 1100}px">
        <span class="chip"><b>0</b> dependencies</span><span class="chip"><b>~30 MB</b> RAM</span>
        <span class="chip"><b>signed</b> webhook</span><span class="chip"><b>no</b> Android device needed</span><span class="chip"><b>MIT</b></span>
      </div>
    </div>{notif}{waves}"""
    return page(w, h, body)


def proof(data):
    rows = []
    for e in data["events"]:
        g, u = e["google"], e["usgs"]
        when = g["origin_utc"][:10]
        place = {"8e683": "Santander, Colombia", "32f7b": "Mindanao, Philippines", "8fadf": "Southern Panama",
                 "8fae1": "South of Panama", "8fad9": "Southern Panama"}[e["google_event_id"][:5]]
        ref = (f'<span class="ok">✓ USGS {u["id"]}</span> <span class="soft">M{u["magnitude"]}</span>' if u
               else '<span class="warn">? no catalogue match</span>')
        rows.append(f"""<tr><td class="mono soft">{when}</td><td>{html.escape(place)}</td>
            <td class="mono">M{g['magnitude']}</td><td class="mono" style="font-size:15px">{ref}</td>
            <td class="mono cy" style="text-align:right">{e['latency']['received_after_google_issue_s']:.2f} s</td></tr>""")
    css = """table{width:100%;border-collapse:collapse;font-size:19px} td{padding:13px 10px;border-top:1px solid var(--line)}
             tr:first-child td{border-top:0} th{font-family:'Geist Mono',monospace;font-size:12px;color:var(--dim);letter-spacing:.08em;
             text-align:left;font-weight:500;padding:0 10px 10px}"""
    stat = lambda big, small, cls="": (f'<div class="card" style="padding:20px 24px;flex:1"><div class="{cls}" style="font-size:40px;'
                                       f'font-weight:700;letter-spacing:-.02em">{big}</div><div class="mono soft" style="font-size:14px;margin-top:6px">{small}</div></div>')
    body = f"""<div class="wrap">
      <div class="kicker">FIELD RESULTS · OCTOBER 2026</div>
      <h1 style="font-size:52px;margin-top:14px">5 real earthquake alerts from Google.<br><span class="soft">No Android device. Just a Raspberry Pi.</span></h1>
      <div style="display:flex;gap:16px;margin-top:30px">
        {stat('4 / 5', 'confirmed by USGS', 'ok')}{stat('0.16–0.30 s', 'after Google issued each alert', 'cy')}{stat('up to ~70 s', 'before the S-wave, at the alert edge')}
      </div>
      <div class="card" style="margin-top:22px;padding:18px 18px 8px">
        <table><tr><th>DATE (UTC)</th><th>WHERE</th><th>GOOGLE</th><th>CHECKED AGAINST</th><th style="text-align:right">OUR DELAY</th></tr>{''.join(rows)}</table>
      </div>
    </div>"""
    return page(1270, 760, body, css)


def lead_time(data):
    e = next(x for x in data["events"] if x["google_event_id"].startswith("8fad9"))
    u, g = e["usgs"], e["google"]
    lat0, lon0 = u["lat"], u["lon"]
    kx, ky = math.cos(math.radians(lat0)) * 111.32, 110.57
    S, cx, cy = 2.75, 330, 350                      # px per km, centre of the plot
    proj = lambda la, lo: (cx + (lo - lon0) * kx * S, cy - (la - lat0) * ky * S)
    colors = {"MMI5+": "#ef4444", "MMI4": "#eab308", "MMI3": "#38bdf8"}
    polys, labels = [], []
    for c in reversed(g["alert_contours"]):          # outer first
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (proj(a, b) for a, b in c["vertices"]))
        col = colors[c["level"]]
        polys.append(f'<polygon points="{pts}" fill="{col}" fill-opacity=".10" stroke="{col}" stroke-width="2.5"/>')
        lt = [f["lead_time_s"] for f in c["lead_time_at_edge"]]
        dk = [f["epicentral_km"] for f in c["lead_time_at_edge"]]
        if max(lt) <= 0:
            txt, cls = "no warning · blind zone", "bad"
        else:
            txt, cls = f"{min(lt):+.0f} to {max(lt):+.0f} s before shaking", ("warn" if c["level"] == "MMI4" else "cy")
        labels.append((c["level"], col, f"{dk[0]:.0f}–{dk[1]:.0f} km", txt, cls))
    ux, uy = proj(lat0, lon0)
    gx, gy = proj(g["lat"], g["lon"])
    bar = 50 * S
    svg = f"""<svg width="660" height="700" style="position:absolute;left:24px;top:40px">
      {''.join(polys)}
      <circle cx="{gx:.1f}" cy="{gy:.1f}" r="6" fill="#e6e9ef"/>
      <path d="M{ux:.1f},{uy-11:.1f} L{ux+3.2:.1f},{uy-3.5:.1f} L{ux+11:.1f},{uy-3.4:.1f} L{ux+4.8:.1f},{uy+1.6:.1f} L{ux+7:.1f},{uy+9.5:.1f} L{ux:.1f},{uy+5:.1f} L{ux-7:.1f},{uy+9.5:.1f} L{ux-4.8:.1f},{uy+1.6:.1f} L{ux-11:.1f},{uy-3.4:.1f} L{ux-3.2:.1f},{uy-3.5:.1f} Z" fill="#22c55e"/>
      <line x1="40" y1="660" x2="{40+bar:.1f}" y2="660" stroke="#8a94a6" stroke-width="2"/>
      <text x="40" y="648" fill="#8a94a6" font-family="Geist Mono" font-size="14">50 km</text>
    </svg>"""
    legend = "".join(f"""<div style="display:flex;gap:16px;align-items:flex-start;margin-top:22px">
        <span style="width:16px;height:16px;border-radius:4px;background:{col};margin-top:7px;flex:none"></span>
        <div><div style="font-size:23px;font-weight:600">{lvl.replace('MMI', 'MMI ')} <span class="mono soft" style="font-size:15px;font-weight:400">{dist}</span></div>
        <div class="{cls}" style="font-size:20px;margin-top:4px">{txt}</div></div></div>""" for lvl, col, dist, txt, cls in reversed(labels))
    body = f"""{svg}<div style="position:absolute;left:700px;top:56px;width:520px;z-index:1">
      <div class="kicker">HOW MUCH WARNING? · REAL ALERT</div>
      <h1 style="font-size:44px;margin-top:14px">Southern Panama, M{u['magnitude']}<br><span class="soft">31 Oct 2026</span></h1>
      <div class="mono soft" style="font-size:15px;margin-top:14px;line-height:1.6">Google's alert areas, as received.<br>
        Alert in hand {e['latency']['received_after_reference_origin_s']:.1f} s after the origin (USGS {u['id']}).</div>
      {legend}
      <div class="mono soft" style="font-size:14px;margin-top:28px;line-height:1.6">
        <span style="color:#e6e9ef">●</span> Google epicentre &nbsp; <span class="ok">★</span> USGS epicentre<br>
        Like every early-warning system: nothing at the centre,<br>tens of seconds farther out.</div>
    </div>"""
    return page(1270, 760, body)


def architecture():
    src = [("Official early warnings", "JMA · CENC via Wolfx", "seconds", "ok"),
           ("Google Android alerts", "MCS push channel · S2 cells", "seconds", "ok"),
           ("EMSC · USGS · SGC", "worldwide & national feeds", "minutes", "soft"),
           ("Your own seismometer", "Raspberry Shake · P-wave trigger", "on site", "ok")]
    dst = [("Home Assistant", "configs included"), ("Node-RED / n8n", "any webhook flow"),
           ("Your app or server", "REST API + webhook"), ("Sirens, lights, valves", "whatever you wire up")]
    box = lambda t, s, right="": (f'<div class="card" style="padding:16px 20px;margin-bottom:14px"><div style="display:flex;justify-content:space-between;'
                                  f'align-items:baseline"><span style="font-size:21px;font-weight:600">{t}</span>{right}</div>'
                                  f'<div class="mono soft" style="font-size:14px;margin-top:4px">{s}</div></div>')
    left = "".join(box(t, s, f'<span class="mono {c}" style="font-size:13px">{sp}</span>') for t, s, sp, c in src)
    right = "".join(box(t, s) for t, s in dst)
    arrow = '<div class="cy" style="font-size:44px;align-self:center;padding:0 10px">→</div>'
    desk = """<div class="card" style="padding:26px 24px;border-color:rgba(56,189,248,.5);box-shadow:0 0 60px rgba(56,189,248,.12);align-self:center;width:330px">
      <div class="kicker">THE BRIDGE</div><div style="font-size:26px;font-weight:700;margin-top:8px">One Python file</div>
      <ul class="mono" style="list-style:none;font-size:14px;color:var(--muted);margin-top:16px;line-height:2">
        <li><span class="cy">›</span> intensity at your place (MMI)</li><li><span class="cy">›</span> S-wave countdown</li>
        <li><span class="cy">›</span> one event per quake</li><li><span class="cy">›</span> drops test & crisis alerts</li>
        <li><span class="cy">›</span> HMAC-signed webhook</li></ul></div>"""
    body = f"""<div class="wrap">
      <div class="kicker">HOW IT FITS</div>
      <h1 style="font-size:48px;margin-top:14px">Every source in, <span class="soft">one webhook out.</span></h1>
      <div style="display:flex;gap:6px;margin-top:40px;align-items:stretch">
        <div style="width:365px">{left}</div>{arrow}{desk}{arrow}<div style="width:290px">{right}</div>
      </div>
      <div style="display:flex;gap:12px;margin-top:34px;flex-wrap:wrap">
        <span class="chip"><b>no</b> Android device needed</span><span class="chip"><b>0</b> dependencies · Python 3.8+</span>
        <span class="chip"><b>~30 MB</b> RAM · runs on a Raspberry Pi</span><span class="chip"><b>MIT</b></span>
      </div>
    </div>"""
    return page(1270, 760, body)


def find_chrome(explicit):
    for c in ([explicit] if explicit else []) + CHROME_CANDIDATES:
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c if os.path.isfile(c) else shutil.which(c)
    sys.exit("Chrome/Chromium not found; pass --chrome PATH")


def render(chrome, html_text, out, w, h, tmp):
    src = os.path.join(tmp, os.path.basename(out) + ".html")
    open(src, "w", encoding="utf-8").write(html_text)
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    f"--window-size={w},{h}", "--virtual-time-budget=6000", f"--screenshot={out}", "file://" + src],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("wrote", os.path.relpath(out, os.path.dirname(DOCS)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome")
    a = ap.parse_args()
    chrome = find_chrome(a.chrome)
    data = json.load(open(DATA, encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        render(chrome, hero(1270, 760), os.path.join(HERE, "gallery-1-hero.png"), 1270, 760, tmp)
        render(chrome, proof(data), os.path.join(HERE, "gallery-2-field-results.png"), 1270, 760, tmp)
        render(chrome, lead_time(data), os.path.join(HERE, "gallery-3-lead-time-panama.png"), 1270, 760, tmp)
        render(chrome, architecture(), os.path.join(HERE, "gallery-4-how-it-fits.png"), 1270, 760, tmp)
        render(chrome, hero(1200, 630, gallery=False), os.path.join(DOCS, "og.png"), 1200, 630, tmp)


if __name__ == "__main__":
    main()
