"""
Flask application for Research Paper Error Checker.
"""
import os
import uuid
import json
import threading
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
from pdf_processor import process_pdf, AVAILABLE_CHECKS, ALL_SECTIONS
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['PROCESSED_FOLDER'] = 'processed'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PROCESSED_FOLDER'], exist_ok=True)

FORMATS_FILE = Path(__file__).parent / "formats.json"

# Legacy synchronous result store (kept for /download and /results)
processing_results = {}

# Async job tracking  {job_id: {"checks": [...], "done": bool, "error": str|None}}
job_progress: dict = {}
job_results: dict = {}


def _build_job_result(job_id, errors, annotated_path, statistics, extracted_data,
                      reference_analysis, original_filename, output_filename,
                      required_sections, start_page):
    """Assemble the JSON payload that /api/result and /upload both return."""
    error_list = [
        {
            'check_id':    e.check_id,
            'check_name':  e.check_name,
            'description': e.description,
            'page_num':    e.page_num + 1,
            'text':        e.text,
            'error_type':  e.error_type,
        }
        for e in errors
    ]
    overview = _build_document_overview(extracted_data, original_filename, statistics, len(errors))
    return {
        'job_id':            job_id,
        'original_filename': original_filename,
        'output_filename':   output_filename,
        'input_path':        None,   # omit paths from API responses
        'output_path':       annotated_path,
        'start_page':        start_page,
        'errors':            error_list,
        'error_count':       len(errors),
        'statistics':        statistics,
        'reference_analysis': reference_analysis,
        'mandatory_sections': required_sections,
        'document_overview': overview,
        'processed_at':      datetime.now().isoformat(),
        'success':           True,
    }


def _run_processing_job(job_id, input_path, output_path, required_sections,
                        enabled_check_types, start_page, original_filename,
                        output_filename):
    """Background worker: process PDF and store result in job_results."""
    def progress_callback(check_key: str, check_name: str):
        job_progress[job_id]["checks"].append({"key": check_key, "name": check_name})

    try:
        print(f"[JOB {job_id}] Starting processing…")
        errors, annotated_path, statistics, extracted_data, reference_analysis = process_pdf(
            input_path, output_path,
            required_sections=required_sections or None,
            enabled_check_types=enabled_check_types,
            start_page=start_page,
            progress_callback=progress_callback,
        )
        print(f"[JOB {job_id}] Complete — {len(errors)} errors")

        # Persist extracted data
        json_path = os.path.join(app.config['PROCESSED_FOLDER'],
                                 f"{job_id}_extracted_data.json")
        with open(json_path, 'w', encoding='utf-8') as jf:
            json.dump(extracted_data, jf, indent=2, ensure_ascii=False)

        result = _build_job_result(
            job_id, errors, annotated_path, statistics, extracted_data,
            reference_analysis, original_filename, output_filename,
            required_sections, start_page,
        )
        # Keep legacy store for /download
        processing_results[job_id] = {**result,
                                       'input_path': input_path,
                                       'output_path': output_path}
        job_results[job_id] = result
        job_progress[job_id]["done"] = True

    except Exception as exc:
        import traceback
        print(f"[JOB {job_id}] ERROR: {exc}")
        traceback.print_exc()
        job_progress[job_id]["error"] = str(exc)
        job_progress[job_id]["done"] = True


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'pdf'


def _build_document_overview(extracted_data, original_filename, statistics, error_count):
    """Build a lightweight, UI-friendly summary from extracted data."""
    full_text = (extracted_data or {}).get("full_text", "") or ""
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]

    title = lines[0] if lines else original_filename
    # Heuristic: use the second non-empty line if it looks like author metadata.
    authors = "Not detected"
    if len(lines) > 1 and len(lines[1]) < 140:
        authors = lines[1]

    abstract = "Not detected"
    lower_text = full_text.lower()
    abstract_idx = lower_text.find("abstract")
    if abstract_idx >= 0:
        snippet = full_text[abstract_idx: abstract_idx + 1200]
        parts = snippet.split("\n", 1)
        abstract_candidate = parts[1] if len(parts) > 1 else snippet
        abstract = " ".join(abstract_candidate.split())[:550] or "Not detected"

    stats = statistics or {}
    insights = [
        f"{error_count} formatting issue(s) detected",
        f"{stats.get('total_figures', 0)} figure(s) and {stats.get('total_tables', 0)} table(s) identified",
        f"{stats.get('total_equations', 0)} equation(s) detected",
    ]

    return {
        "title": title[:200],
        "authors": authors[:240],
        "abstract": abstract,
        "key_insights": insights,
    }


def load_formats():
    if FORMATS_FILE.exists():
        with open(FORMATS_FILE) as f:
            return json.load(f).get("formats", [])
    return []


def save_formats(formats):
    with open(FORMATS_FILE, "w") as f:
        json.dump({"formats": formats}, f, indent=2)


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    checks_by_cat = {}
    for cid, info in AVAILABLE_CHECKS.items():
        checks_by_cat.setdefault(info["category"], []).append(info["name"])
    return render_template('home.html', checks_by_cat=checks_by_cat)


@app.route('/professor')
def professor():
    checks_by_cat = {}
    for cid, info in AVAILABLE_CHECKS.items():
        checks_by_cat.setdefault(info["category"], []).append({
            "id": cid, **info,
        })
    return render_template('professor.html',
                           all_sections=ALL_SECTIONS,
                           checks_by_cat=checks_by_cat,
                           formats=load_formats())


@app.route('/student')
def student():
    return render_template('student.html', formats=load_formats())


# ── Format API ────────────────────────────────────────────────────────────────

@app.route('/api/formats', methods=['GET'])
def list_formats():
    return jsonify(load_formats())


@app.route('/api/formats', methods=['POST'])
def create_format():
    data = request.get_json(force=True)
    if not data.get('name') or not data.get('created_by'):
        return jsonify({'error': 'name and created_by are required'}), 400

    new_fmt = {
        "id":                 str(uuid.uuid4()),
        "name":               data["name"].strip(),
        "created_by":         data["created_by"].strip(),
        "is_system":          False,
        "description":        data.get("description", "").strip(),
        "mandatory_sections": data.get("mandatory_sections", []),
        "enabled_checks":     data.get("enabled_checks", []),
    }
    formats = load_formats()
    formats.append(new_fmt)
    save_formats(formats)
    return jsonify(new_fmt), 201


@app.route('/api/formats/<fmt_id>', methods=['DELETE'])
def delete_format(fmt_id):
    formats = load_formats()
    updated = [f for f in formats if f["id"] != fmt_id or f.get("is_system")]
    if len(updated) == len(formats):
        return jsonify({'error': 'Format not found or is a system format'}), 404
    save_formats(updated)
    return jsonify({'success': True})


# ── Upload & Process ──────────────────────────────────────────────────────────

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'Only PDF files are allowed'}), 400

    format_id  = request.form.get('format_id', '')
    start_page = max(1, int(request.form.get('start_page', '1') or '1'))
    required_sections  = []
    enabled_check_types = None

    if format_id:
        formats = load_formats()
        fmt = next((f for f in formats if f["id"] == format_id), None)
        if fmt:
            required_sections = fmt.get("mandatory_sections", [])
            enabled_checks    = fmt.get("enabled_checks", [])
            if enabled_checks:
                types = {"missing_required_section"}
                for cid in enabled_checks:
                    if cid in AVAILABLE_CHECKS:
                        types.update(AVAILABLE_CHECKS[cid]["error_types"])
                enabled_check_types = types

    try:
        job_id           = str(uuid.uuid4())
        original_filename = secure_filename(file.filename)
        input_path       = os.path.join(app.config['UPLOAD_FOLDER'],
                                        f"{job_id}_{original_filename}")
        file.save(input_path)

        output_filename = f"annotated_{original_filename}"
        output_path     = os.path.join(app.config['PROCESSED_FOLDER'],
                                       f"{job_id}_{output_filename}")

        # Initialise progress slot before the thread starts so the polling
        # endpoint never sees a missing key.
        job_progress[job_id] = {"checks": [], "done": False, "error": None}

        thread = threading.Thread(
            target=_run_processing_job,
            args=(job_id, input_path, output_path, required_sections,
                  enabled_check_types, start_page, original_filename, output_filename),
            daemon=True,
        )
        thread.start()
        print(f"[UPLOAD] Job {job_id} queued (format={format_id or 'none'}, start_page={start_page})")

        return jsonify({"job_id": job_id, "success": True}), 202

    except Exception as e:
        print(f"[UPLOAD] ERROR: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': f'Upload failed: {str(e)}'}), 500


@app.route('/api/progress/<job_id>')
def get_job_progress(job_id):
    """Poll endpoint: returns completed check list + done/error flags."""
    progress = job_progress.get(job_id)
    if progress is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(progress)


@app.route('/api/result/<job_id>')
def get_job_result(job_id):
    """Return the full result once processing is complete."""
    result = job_results.get(job_id)
    if result:
        return jsonify(result)
    progress = job_progress.get(job_id)
    if progress is None:
        return jsonify({"error": "Job not found"}), 404
    if progress.get("error"):
        return jsonify({"error": progress["error"]}), 500
    return jsonify({"error": "Result not ready yet"}), 202


@app.route('/download/<job_id>')
def download_file(job_id):
    if job_id not in processing_results:
        return jsonify({'error': 'Job not found'}), 404
    result = processing_results[job_id]
    if not os.path.exists(result['output_path']):
        return jsonify({'error': 'Processed file not found'}), 404
    return send_file(result['output_path'], as_attachment=True,
                     download_name=result['output_filename'],
                     mimetype='application/pdf')


@app.route('/results/<job_id>')
def get_results(job_id):
    if job_id not in processing_results:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(processing_results[job_id])


@app.route('/health')
def health():
    return jsonify({'status': 'healthy'})


if __name__ == '__main__':
    print("Starting Research Paper Error Checker…")
    print("Open your browser to: http://localhost:7860")
    app.run(host='0.0.0.0', port=7860, threaded=True)
