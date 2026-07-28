#!/usr/bin/env bash
# Markdown -> PDF, with the Mermaid diagrams pre-rendered.
#
# Pandoc does not know what a ```mermaid fence is; left alone it prints the
# source. So each diagram is rendered to SVG first and the fence replaced with
# an image reference.
#
# Two portability notes, both found the hard way:
#   - mermaid-cli drives headless Chromium, which fails on Ubuntu 23.10+ with
#     "No usable sandbox". Hence the puppeteer config.
#   - Dollar signs are escaped in the source (previewers parse $...$ as LaTeX
#     maths). Pandoc needs them unescaped again, and maths disabled outright.
#
#   ./scripts/build_pdf.sh RESPONSE.md
set -euo pipefail

SRC="${1:-RESPONSE.md}"
OUT="${2:-${SRC%.md}.pdf}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v mmdc >/dev/null || { echo "need mermaid-cli: npm i -g @mermaid-js/mermaid-cli"; exit 1; }
command -v pandoc >/dev/null || { echo "need pandoc"; exit 1; }

echo '{"args":["--no-sandbox","--disable-setuid-sandbox"]}' > "$WORK/puppeteer.json"

python3 - "$SRC" "$WORK" <<'PY'
import re, subprocess, sys, pathlib

src, work = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
text = src.read_text()
count = 0

def render(match):
    global count
    count += 1
    mmd = work / f"d{count}.mmd"
    svg = work / f"d{count}.svg"
    mmd.write_text(match.group(1))
    subprocess.run(
        ["mmdc", "-p", str(work / "puppeteer.json"), "-i", str(mmd), "-o", str(svg),
         "-b", "white", "-w", "1400"],
        check=True, capture_output=True,
    )
    return f"\n![]({svg})\n"

text = re.sub(r"```mermaid\n(.*?)```", render, text, flags=re.S)
text = text.replace(r"\$", "$")          # unescape for pandoc
(work / "body.md").write_text(text)
print(f"rendered {count} diagrams")
PY

pandoc "$WORK/body.md" \
  -o "$OUT" \
  --pdf-engine=wkhtmltopdf \
  --pdf-engine-opt=--enable-local-file-access \
  --pdf-engine-opt=--margin-top   --pdf-engine-opt=18mm \
  --pdf-engine-opt=--margin-bottom --pdf-engine-opt=18mm \
  --pdf-engine-opt=--margin-left  --pdf-engine-opt=16mm \
  --pdf-engine-opt=--margin-right --pdf-engine-opt=16mm \
  --metadata title="" \
  --toc --toc-depth=2 \
  -f markdown-tex_math_dollars-raw_tex \
  --resource-path="$WORK"

echo "wrote $OUT"
