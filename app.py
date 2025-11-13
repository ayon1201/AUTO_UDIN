import os
import threading
import time
import uuid
from queue import Queue

from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for
from werkzeug.utils import secure_filename

from UDIN_V2.worker2 import SeleniumWorker

UPLOAD_FOLDER = "uploads"
DOWNLOAD_FOLDER = "downloads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["DOWNLOAD_FOLDER"] = DOWNLOAD_FOLDER

# Global job manager
jobs = {}  # job_id -> job info dict
jobs_lock = threading.Lock()

# One SeleniumWorker per job (simple approach)
workers = {}  # job_id -> SeleniumWorker instance

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    """
    Upload Excel (udins.xlsx). Expect column 'UDIN'.
    Optional form fields for static details.
    """
    f = request.files.get("file")
    if not f:
        return "No file uploaded", 400
    filename = secure_filename(f.filename)
    path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    f.save(path)

    # read static fields from form
    auth_type = request.form.get("authority_type", "Others")
    auth_name = request.form.get("authority_name", "Ashish")
    mobile = request.form.get("mobile", "8169943760")
    email = request.form.get("email", "ashishkumarmihi@bharatpetroleum.in")

    job_id = str(uuid.uuid4())
    info = {
        "id": job_id,
        "file": path,
        "status": "uploaded",
        "current": None,
        "progress": 0,
        "total": 0,
        "messages": [],
        "captcha_b64": None,
        "awaiting_captcha": False,
        "awaiting_otp": False,
        "last_pdf": None
    }
    with jobs_lock:
        jobs[job_id] = info

    # create and start worker thread
    worker = SeleniumWorker(job_id=job_id,
                            excel_path=path,
                            download_dir=app.config["DOWNLOAD_FOLDER"],
                            update_callback=lambda d: update_job(job_id, d),
                            static_values={
                                "authority_type": auth_type,
                                "authority_name": auth_name,
                                "mobile": mobile,
                                "email": email
                            })
    workers[job_id] = worker
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()

    return redirect(url_for("status", job_id=job_id))


@app.route("/status/<job_id>")
def status(job_id):
    if job_id not in jobs:
        return "Unknown job", 404
    return render_template("status.html", job_id=job_id)


@app.route("/job_info/<job_id>")
def job_info(job_id):
    with jobs_lock:
        info = jobs.get(job_id)
        if not info:
            return jsonify({"error": "job not found"}), 404
        return jsonify(info)


@app.route("/submit_captcha/<job_id>", methods=["POST"])
def submit_captcha(job_id):
    data = request.get_json(force=True)
    val = data.get("captcha")
    if not val:
        return jsonify({"error": "no captcha"}), 400
    w = workers.get(job_id)
    if not w:
        return jsonify({"error": "worker not found"}), 404
    w.provide_captcha(val)
    return jsonify({"status": "ok"})
@app.route("/submit_otp/<job_id>", methods=["POST"])
def submit_otp(job_id):
    data = request.get_json(force=True)
    val = data.get("otp")
    if not val:
        return jsonify({"error": "no otp"}), 400
    w = workers.get(job_id)
    if not w:
        return jsonify({"error": "worker not found"}), 404
    w.provide_otp(val)
    return jsonify({"status": "ok"})
@app.route("/submit_otp_mobile/<job_id>", methods=["POST"])
def submit_otp_mobile(job_id):
    data = request.get_json(force=True)
    otp = data.get("otp")
    if not otp:
        return jsonify({"error": "no mobile otp"}), 400
    w = workers.get(job_id)
    if not w:
        return jsonify({"error": "worker not found"}), 404
    w.provide_mobile_otp(otp)
    return jsonify({"status": "ok", "type": "mobile"})


@app.route("/submit_otp_email/<job_id>", methods=["POST"])
def submit_otp_email(job_id):
    data = request.get_json(force=True)
    otp = data.get("otp")
    if not otp:
        return jsonify({"error": "no email otp"}), 400
    w = workers.get(job_id)
    if not w:
        return jsonify({"error": "worker not found"}), 404
    w.provide_email_otp(otp)
    return jsonify({"status": "ok", "type": "email"})


@app.route("/receive_otp", methods=["POST"])
def receive_otp():
    """
    MacroDroid should POST JSON: {"body":"Your OTP is 1234", "from":"+91...","job_id":"<optional>"}
    If job_id not provided we put to the most recent job awaiting OTP.
    """
    data = request.get_json(force=True) or {}
    text = data.get("body") or data.get("text") or ""
    job_id = data.get("job_id")
    # extract digits
    import re
    m = re.search(r"\b(\d{4,8})\b", text)
    if not m:
        return jsonify({"status": "no_otp"}), 200
    otp = m.group(1)

    # pick job
    target_job = None
    if job_id and job_id in workers:
        target_job = workers[job_id]
    else:
        # find the most recent worker that is waiting for OTP
        for w in workers.values():
            if w.is_waiting_for_otp():
                target_job = w
                break

    if not target_job:
        return jsonify({"status": "no_waiting_job"}), 200

    target_job.provide_otp(otp)
    return jsonify({"status": "ok", "otp": otp}), 200


@app.route("/download/<filename>")
def download_file(filename):
    return send_from_directory(app.config["DOWNLOAD_FOLDER"], filename, as_attachment=True)


def update_job(job_id, data: dict):
    """Callback for worker to update job info."""
    with jobs_lock:
        info = jobs.get(job_id)
        if not info:
            return
        info.update(data)
        # ensure messages capped
        if "message" in data:
            info.setdefault("messages", []).append(data["message"])
            if len(info["messages"]) > 200:
                info["messages"] = info["messages"][-200:]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Flask app on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)

