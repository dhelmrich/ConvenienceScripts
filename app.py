import os
import json
import tempfile
import subprocess
from flask import Flask, request, jsonify, render_template, send_file
import base64
from fs.memoryfs import MemoryFS

app = Flask(__name__)

BASE_DIR = os.path.dirname(__file__)
TEMPLATE_PATH = os.path.join(BASE_DIR, "base.txt")
CONFIG_PATH = os.path.join(BASE_DIR, "system.json")
TEMPLATE_KEY = "CONTENTOFEQUATION"

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = load_config()
PDFLATEX = CONFIG.get("pdflatex", "pdflatex")
PDFCROP = CONFIG.get("pdfcrop", "pdfcrop")
INKSCAPE = CONFIG.get("inkscape", "inkscape")

# Simple in-memory cache
_render_cache = {}

def render_latex(latex_str):
    key = latex_str
    if key in _render_cache:
        return _render_cache[key]
    # Use temporary directory for rendering
    with tempfile.TemporaryDirectory() as tmpdir:
        memfs = MemoryFS()
        # Read template into memory FS
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            template = f.read()
        memfs.writetext('base.txt', template)
        tex_content = memfs.readtext('base.txt').replace(TEMPLATE_KEY, latex_str)
        memfs.writetext('equation.tex', tex_content)
        # Write tex from memory FS to real file for pdflatex
        tex_path = os.path.join(tmpdir, "equation.tex")
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(memfs.readtext('equation.tex'))
        # Compile with pdflatex
        result = subprocess.run(
            [PDFLATEX, "-interaction=nonstopmode", "equation.tex"],
            cwd=tmpdir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        pdf_path = os.path.join(tmpdir, "equation.pdf")
        if not os.path.exists(pdf_path):
            raise RuntimeError("pdflatex failed to produce PDF")
        # Crop twice as in original
        for _ in range(2):
            subprocess.run([PDFCROP, "equation.pdf", "equation-crop.pdf"], cwd=tmpdir)
            os.replace(os.path.join(tmpdir, "equation-crop.pdf"), pdf_path)
        # Export SVG via inkscape
        svg_path = os.path.join(tmpdir, "equation.svg")
        subprocess.run(
            [INKSCAPE, "--export-type=svg", "--export-filename=equation.svg", "equation.pdf"],
            cwd=tmpdir,
        )
        # Read outputs
        with open(svg_path, "r", encoding="utf-8") as f:
            svg_data = f.read()
        with open(pdf_path, "rb") as f:
            pdf_data = f.read()
        # Store outputs in MemoryFS for intermediate handling
        memfs.writetext('equation.svg', svg_data)
        memfs.writebytes('equation.pdf', pdf_data)
        svg_data = memfs.readtext('equation.svg')
        pdf_data = memfs.readbytes('equation.pdf')
        result = (svg_data, pdf_data)
        _render_cache[key] = result
        return result

def pdf_to_png(pdf_bytes, scale):
    # Render PNG from PDF bytes at DPI scaled
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "equation.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        png_path = os.path.join(tmpdir, "equation.png")
        dpi = int(300 * scale)
        # Cap DPI to avoid excessive size
        dpi = max(72, min(dpi, 2400))
        subprocess.run(
            [INKSCAPE, "--export-type=png", f"--export-filename=equation.png", f"--export-dpi={dpi}", "equation.pdf"],
            cwd=tmpdir,
        )
        with open(png_path, "rb") as f:
            return f.read()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/render", methods=["POST"])
def api_render():
    data = request.get_json()
    latex = data.get("latex", "")
    scale = float(data.get("scale", 1))
    invert = bool(data.get("invert", False))
    try:
        svg_data, pdf_data = render_latex(latex)
        # Generate PNG at appropriate DPI, no LaTeX re-render
        png_bytes = pdf_to_png(pdf_data, scale)
        if invert:
            # SVG invert via CSS filter markup
            if '<svg' in svg_data:
                svg_data = svg_data.replace('<svg', '<svg style="filter:invert(1)"', 1)
            # PNG invert opaque colors
            from PIL import Image, ImageOps
            import io
            if png_bytes:
                with Image.open(io.BytesIO(png_bytes)) as img:
                    if img.mode != 'RGBA':
                        img = img.convert('RGBA')
                    inv = ImageOps.invert(img.convert('RGB')).convert('RGBA')
                    alpha = img.split()[-1]
                    inv.putalpha(alpha)
                    buf = io.BytesIO()
                    inv.save(buf, format='PNG')
                    png_bytes = buf.getvalue()
        svg_b64 = base64.b64encode(svg_data.encode("utf-8")).decode("utf-8")
        png_b64 = base64.b64encode(png_bytes).decode("utf-8") if png_bytes else ""
        pdf_b64 = base64.b64encode(pdf_data).decode("utf-8")
        return jsonify({
            "svg": svg_b64,
            "png": png_b64,
            "pdf": pdf_b64,
            "svg_text": svg_data
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
