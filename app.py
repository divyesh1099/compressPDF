# app.py
"""
High-quality Multi-PDF Compressor (Flask)
  - Compresses multiple PDFs in one batch with best-in-class techniques
  - Cleans compressed directory before each compression for freshness
  - PyMuPDF (lossy recompress), PikePDF/qpdf, and Ghostscript (aggressive)
  - Works for single or multiple PDF uploads
Author: Divyesh & ChatGPT, 2025
"""

import os, uuid, logging, subprocess, pathlib, mimetypes, shutil
from flask import (
    Flask, request, send_file, render_template,
    flash, redirect, url_for, send_from_directory
)
import fitz
import pikepdf
from typing import Dict, Any
from werkzeug.utils import secure_filename

import io
from zipfile import ZipFile
# ──────────────── Config & Globals ────────────────
app               = Flask(__name__)
UPLOAD_DIR        = pathlib.Path("uploads")
STAGE_DIR         = pathlib.Path("stage")       # intermediate temp
OUT_DIR           = pathlib.Path("compressed")
for d in (UPLOAD_DIR, STAGE_DIR, OUT_DIR): d.mkdir(exist_ok=True)
app.secret_key    = "CHANGE_ME_FOR_PROD"
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024          # 200 MB total
ALLOWED           = {"pdf"}

# Ghostscript quality presets
GS_QUALITY_MAP = {
    "screen":  "/screen",   # 72 dpi images – smallest
    "ebook":   "/ebook",    # 150 dpi – good balance
    "printer": "/printer",  # 300 dpi – HQ print
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ────────────── Helpers & Optimisers ──────────────

def allowed_file(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED

def clean_directory(dir_path: pathlib.Path):
    """Deletes all files in a directory (not subdirs)."""
    if dir_path.exists():
        for file in dir_path.iterdir():
            if file.is_file():
                try:
                    file.unlink()
                except Exception as ex:
                    logging.error(f"Error deleting {file}: {ex}")

def recompress_images_with_pymupdf(pdf_in: pathlib.Path,
                                   pdf_out: pathlib.Path,
                                   jpeg_q: int = 75) -> None:
    """Lossily recompress RGB/Gray images if new size < original."""
    doc = fitz.open(pdf_in)
    recompressed, skipped, larger = 0, 0, 0

    for page in doc:
        for xref, *_ in page.get_images(full=True):
            try:
                raw   = doc.extract_image(xref)
                if not raw: continue
                raw_bytes = raw["image"]
                raw_sz    = len(raw_bytes)
                pix = fitz.Pixmap(doc, xref)
                if pix.alpha:  # Ignore images with alpha channel (CMYK etc)
                    skipped += 1; continue
                new_bytes = pix.tobytes("jpeg", jpeg_quality=jpeg_q)
                if len(new_bytes) < raw_sz:
                    doc.replace_image(xref, stream=new_bytes)
                    recompressed += 1
                else:
                    larger += 1
            except Exception as ex:
                logging.debug("Image-recompress error: %s", ex)
                skipped += 1
    doc.save(pdf_out, garbage=4, deflate=True, clean=True)
    doc.close()
    logging.info("PyMuPDF pass – recompressed:%d  kept-larger:%d  skipped:%d",
                 recompressed, larger, skipped)

def qpdf_optimize(pdf_in: pathlib.Path,
                  pdf_out: pathlib.Path) -> None:
    """Remove duplicates, compress streams, object streams, etc."""
    with pikepdf.open(pdf_in) as pdf:
        kw: Dict[str, Any] = {
            "linearize": False,
            "compress_streams": True,
        }
        if hasattr(pikepdf, "ObjectStreamMode") and \
           hasattr(pikepdf.ObjectStreamMode, "GENERATE"):
            kw["object_stream_mode"] = pikepdf.ObjectStreamMode.GENERATE
        else:
            logging.info("Older pikepdf detected – skipping object-stream generation")
        pdf.save(pdf_out, **kw)

def ghostscript_compress(pdf_in: pathlib.Path,
                         pdf_out: pathlib.Path,
                         quality_key: str = "ebook") -> None:
    """Call Ghostscript for final aggressive compression + linearise."""
    preset = GS_QUALITY_MAP.get(quality_key, "/ebook")
    gs_cmd = [
        "gswin64c" if os.name == "nt" else "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.6",
        f"-dPDFSETTINGS={preset}",
        "-dDetectDuplicateImages=true",
        "-dAutoRotatePages=/None",
        "-dColorImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dMonoImageDownsampleType=/Subsample",
        "-dEmbedAllFonts=true",
        "-dSubsetFonts=true",
        "-dCompressFonts=true",
        "-dFastWebView=true",
        "-dNOPAUSE", "-dBATCH",
        f"-sOutputFile={str(pdf_out)}",
        str(pdf_in),
    ]
    logging.info("Running Ghostscript… (%s)", " ".join(gs_cmd))
    completed = subprocess.run(gs_cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"Ghostscript error: {completed.stderr}")

def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024: return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

# ───────────────────────── Routes ──────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/compress", methods=["POST"])
def compress():
    # ---- Clean compressed directory before every new compress ----
    clean_directory(OUT_DIR)

    # --------- Accept MULTIPLE PDF files -----------
    if "pdf_file" not in request.files:
        flash("No file part", "error")
        return redirect(url_for("index"))
    files = request.files.getlist("pdf_file")
    if not files or all(f.filename == "" for f in files):
        flash("Please select at least one PDF file.", "error")
        return redirect(url_for("index"))

    jpeg_q = int(request.form.get("image_quality", 75))
    jpeg_q = min(max(jpeg_q, 10), 100)
    gs_preset = request.form.get("gs_quality", "ebook")

    results = []  # To store info about each file for results page

    for fh in files:
        if fh.filename == "" or not allowed_file(fh.filename):
            continue  # Skip bad uploads, don't abort whole batch

        uid = uuid.uuid4().hex
        orig_fname = secure_filename(fh.filename)
        in_path  = UPLOAD_DIR / f"{uid}_{orig_fname}"
        stage1   = STAGE_DIR / f"{uid}_pymupdf.pdf"
        stage2   = STAGE_DIR / f"{uid}_qpdf.pdf"
        final_f  = OUT_DIR  / f"{uid}_compressed_{orig_fname}"

        fh.save(in_path)
        logging.info("Uploaded ▸ %s (size=%s)", in_path, human(in_path.stat().st_size))

        try:
            # Pass-1: image recompress
            recompress_images_with_pymupdf(in_path, stage1, jpeg_q)

            # Pass-2: qpdf + pikepdf optimise
            qpdf_optimize(stage1, stage2)

            # Pass-3: Ghostscript heavy compression
            ghostscript_compress(stage2, final_f, gs_preset)

            before, after = in_path.stat().st_size, final_f.stat().st_size
            saved = 100*(before-after)/before
            logging.info("Total reduction: %.1f%%  (%s ► %s)",
                         saved, human(before), human(after))

            # Clean temp
            for p in (in_path, stage1, stage2):
                p.unlink(missing_ok=True)

            results.append({
                "orig_name": orig_fname,
                "token": final_f.name,
                "original_size": before,
                "compressed_size": after,
                "reduction_percent": saved
            })

        except Exception as ex:
            logging.error("Compression failed for %s: %s", orig_fname, ex, exc_info=True)
            flash(f"Compression failed for {orig_fname}: {ex}", "error")
            # Cleanup
            for p in (in_path, stage1, stage2, final_f):
                p.unlink(missing_ok=True)
            continue

    if not results:
        flash("No PDFs were successfully compressed. Please check your files.", "error")
        return redirect(url_for("index"))

    return render_template("success.html", results=results)

@app.route("/download/<filetoken>")
def download(filetoken):
    path = OUT_DIR / filetoken
    if not path.exists():
        flash("File expired or deleted.", "error")
        return redirect(url_for("index"))
    mime, _ = mimetypes.guess_type(str(path)); mime = mime or "application/pdf"
    response = send_file(
        path,
        as_attachment=True,
        download_name=path.name.split("_compressed_",1)[-1],
        mimetype=mime
    )
    @response.call_on_close
    def _auto_delete():
        try: path.unlink(missing_ok=True)
        except Exception: pass
    return response

@app.route("/download-all")
def download_all():
    files = list(OUT_DIR.glob("*_compressed_*"))
    if not files:
        flash("No files available for bulk download.", "error")
        return redirect(url_for("index"))

    # In-memory zip
    zip_buffer = io.BytesIO()
    with ZipFile(zip_buffer, "w") as zipf:
        for file in files:
            # Inside zip, store with original filename
            download_name = file.name.split("_compressed_", 1)[-1]
            zipf.write(file, arcname=download_name)

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        as_attachment=True,
        download_name="compressed_pdfs.zip",
        mimetype="application/zip"
    )

# ─────────────────────────── main ──────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
