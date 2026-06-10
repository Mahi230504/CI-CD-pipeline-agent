#!/usr/bin/env python3
"""Transform the reveal.js masterclass deck into a print-paginated, self-contained
HTML (one slide = one 1280x720 landscape page) suitable for Chrome --print-to-pdf."""
import re, pathlib

SRC = pathlib.Path("MASTERCLASS_DECK.html")
OUT = pathlib.Path("MASTERCLASS_DECK_print.html")

html = SRC.read_text()

# --- 1. pull the inline <style> and de-reveal the selectors -----------------
style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
style = style.replace("html.reveal-full-page, .reveal-viewport{", "body{")
style = style.replace(".reveal {", "body {")
style = style.replace(".reveal ", "")  # blanket: .reveal h1 -> h1, etc.

# --- 2. extract every slide <section> ... </section> ------------------------
sections = re.findall(r"<section\b([^>]*)>(.*?)</section>", html, re.S)

PRINT_CSS = """
@page { size: 1280px 720px; margin: 0; }
* { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
html, body { margin:0; padding:0; width:1280px; }
body { font-size:26px; }
section.page {
  width:1280px; height:720px; box-sizing:border-box;
  padding:46px 66px; overflow:hidden; position:relative;
  text-align:left;
  display:flex; flex-direction:column; justify-content:center;
  page-break-after:always; break-after:page;
  background:
    radial-gradient(1200px 700px at 12% -10%, #15233f 0%, transparent 55%),
    radial-gradient(1000px 600px at 100% 0%, #1a1330 0%, transparent 50%),
    linear-gradient(160deg,#0a0e17,#0f1626);
}
section.page:last-child { page-break-after:avoid; break-after:auto; }
pre code { max-height:500px; }
.mermaid svg { max-height:460px !important; max-width:100% !important; width:auto !important; height:auto !important; }
.mermaid { width:100%; }
"""

def build_section(attrs, inner):
    bg = re.search(r'data-background-color="([^"]+)"', attrs)
    style_attr = f' style="background:{bg.group(1)} !important"' if bg else ""
    return f'<section class="page"{style_attr}>{inner}</section>'

body = "\n".join(build_section(a, i) for a, i in sections)

OUT.write_text(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/reveal.js@5.1.0/plugin/highlight/monokai.css">
<style>{style}
{PRINT_CSS}</style>
</head><body>
{body}
<script src="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/highlight.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/languages/python.min.js"></script>
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs';
mermaid.initialize({{
  startOnLoad:false, theme:'base', securityLevel:'loose',
  flowchart:{{ useMaxWidth:true, htmlLabels:true, curve:'basis' }},
  themeVariables:{{
    fontFamily:'Inter, Segoe UI, sans-serif', fontSize:'15px', lineColor:'#5a6b86',
    primaryColor:'#13203a', primaryTextColor:'#e7ecf5', primaryBorderColor:'#33e1d6',
    secondaryColor:'#1c1430', tertiaryColor:'#0f1626',
    clusterBkg:'rgba(78,168,255,.06)', clusterBorder:'rgba(78,168,255,.35)',
    edgeLabelBackground:'#0f1626'
  }}
}});
if (window.hljs) {{ document.querySelectorAll('pre code').forEach(b => window.hljs.highlightElement(b)); }}
await mermaid.run({{ querySelector: '.mermaid' }});
window.__ready = true;
</script>
</body></html>
""")
print(f"wrote {OUT} ({OUT.stat().st_size//1024} KB, {len(sections)} pages)")
