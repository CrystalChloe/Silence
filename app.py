"""
Shh! - School Quietness Tracker
================================

A small Flask + SQLite app that lets students:
  1. See a live overview of how quiet/loud every spot on campus is right now.
  2. Submit a 1 (silent) - 10 (very loud) rating for a location they're currently in.
  3. See a PREDICTED noise level for locations that have no recent ratings yet,
     so the app is never just blank for a spot nobody has rated today.

Run it with:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000 in a browser.

Everything (backend routes + HTML templates) lives in this one file to keep
the app easy to drop into a single folder and run.
"""

import os
import sqlite3
from datetime import datetime, timedelta
from statistics import mean

from flask import (
    Flask,
    g,
    render_template_string,
    request,
    redirect,
    url_for,
    jsonify,
    send_from_directory,
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
DB_PATH = "quiet_tracker.db"

# --- Photo upload settings --------------------------------------------------
UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB per upload, plenty for a phone photo
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ---------------------------------------------------------------------------
# Starter locations. "category" is used for the prediction fallback (step 3
# below) and "baseline" is the very last-resort guess (step 4) based on what
# that *type* of space is normally like.
# ---------------------------------------------------------------------------
SEED_LOCATIONS = [
    # (name, category, baseline)
    ("Main Library - Quiet Floor", "study", 2),
    ("Library - Group Study Rooms", "study", 5),
    ("Study Hall", "study", 3),
    ("Cafeteria", "social", 8),
    ("Student Lounge", "social", 6),
    ("Gymnasium", "athletics", 8),
    ("Main Hallway", "transit", 6),
    ("Outdoor Courtyard", "outdoor", 4),
    ("Computer Lab", "study", 3),
    ("Auditorium (empty)", "study", 2),
]

RATING_WINDOW_DAYS = 14  # how far back "recent" ratings are considered fresh


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            baseline INTEGER NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER NOT NULL,
            level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 10),
            timestamp TEXT NOT NULL,
            FOREIGN KEY(location_id) REFERENCES locations(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            caption TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(location_id) REFERENCES locations(id)
        )"""
    )
    # Seed locations only if the table is empty
    existing = conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
    if existing == 0:
        conn.executemany(
            "INSERT INTO locations (name, category, baseline) VALUES (?, ?, ?)",
            SEED_LOCATIONS,
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Prediction logic
#
# When a location has no fresh ratings, we fall back through progressively
# broader sources of evidence so the app always shows a sensible number:
#   1. Ratings for THIS location around the SAME hour of day (any past day)
#   2. Any past ratings for THIS location, regardless of time
#   3. Ratings from OTHER locations in the same category (e.g. other "study"
#      spaces), as a proxy
#   4. The location's hand-set baseline guess (last resort)
# ---------------------------------------------------------------------------
def get_level_for_location(location):
    """Returns (level: float, source: str, is_prediction: bool)."""
    conn = get_db()
    loc_id = location["id"]
    now = datetime.now()
    cutoff = (now - timedelta(days=RATING_WINDOW_DAYS)).isoformat()

    # --- Real data: fresh ratings actually submitted for this spot -------
    fresh = conn.execute(
        "SELECT level FROM ratings WHERE location_id=? AND timestamp>=? "
        "ORDER BY timestamp DESC LIMIT 20",
        (loc_id, cutoff),
    ).fetchall()
    if fresh:
        return round(mean(r["level"] for r in fresh), 1), "live student ratings", False

    # --- Prediction step 1: same location, similar hour of day, any date -
    hour = now.hour
    lo, hi = f"{max(0, hour - 1):02d}", f"{min(23, hour + 1):02d}"
    same_hour = conn.execute(
        "SELECT level FROM ratings WHERE location_id=? "
        "AND strftime('%H', timestamp) BETWEEN ? AND ?",
        (loc_id, lo, hi),
    ).fetchall()
    if same_hour:
        return (
            round(mean(r["level"] for r in same_hour), 1),
            "predicted from past ratings at this time of day",
            True,
        )

    # --- Prediction step 2: any historical rating for this location ------
    any_rating = conn.execute(
        "SELECT level FROM ratings WHERE location_id=?", (loc_id,)
    ).fetchall()
    if any_rating:
        return (
            round(mean(r["level"] for r in any_rating), 1),
            "predicted from this location's rating history",
            True,
        )

    # --- Prediction step 3: other locations in the same category ---------
    category_ratings = conn.execute(
        """SELECT r.level FROM ratings r
           JOIN locations l ON r.location_id = l.id
           WHERE l.category=? AND l.id != ?""",
        (location["category"], loc_id),
    ).fetchall()
    if category_ratings:
        return (
            round(mean(r["level"] for r in category_ratings), 1),
            f"predicted from similar '{location['category']}' spaces",
            True,
        )

    # --- Prediction step 4: hand-set baseline default ---------------------
    return float(location["baseline"]), "predicted default for this type of space", True


def level_to_word(level):
    if level <= 2:
        return "Silent"
    if level <= 4:
        return "Quiet"
    if level <= 6:
        return "Moderate"
    if level <= 8:
        return "Loud"
    return "Very Loud"


def level_to_color(level):
    # Green (quiet) -> yellow -> red (loud), simple 3-stop gradient
    if level <= 3:
        return "#2e7d32"
    if level <= 5:
        return "#9e9d24"
    if level <= 7:
        return "#ef6c00"
    return "#c62828"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    conn = get_db()
    locations = conn.execute("SELECT * FROM locations ORDER BY name").fetchall()
    board = []
    for loc in locations:
        level, source, is_pred = get_level_for_location(loc)
        photo_count = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE location_id=?", (loc["id"],)
        ).fetchone()[0]
        board.append(
            {
                "id": loc["id"],
                "name": loc["name"],
                "category": loc["category"],
                "level": level,
                "word": level_to_word(level),
                "color": level_to_color(level),
                "source": source,
                "is_prediction": is_pred,
                "photo_count": photo_count,
            }
        )
    # Quietest first, handy for "find me somewhere to study/sleep"
    board.sort(key=lambda x: x["level"])
    return render_template_string(DASHBOARD_HTML, board=board)


@app.route("/location/<int:location_id>", methods=["GET"])
def location_detail(location_id):
    conn = get_db()
    loc = conn.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
    if loc is None:
        return "Location not found", 404
    level, source, is_pred = get_level_for_location(loc)
    recent = conn.execute(
        "SELECT level, timestamp FROM ratings WHERE location_id=? "
        "ORDER BY timestamp DESC LIMIT 15",
        (location_id,),
    ).fetchall()
    photos = conn.execute(
        "SELECT id, filename, caption, timestamp FROM photos WHERE location_id=? "
        "ORDER BY timestamp DESC",
        (location_id,),
    ).fetchall()
    return render_template_string(
        DETAIL_HTML,
        loc=loc,
        level=level,
        word=level_to_word(level),
        color=level_to_color(level),
        level_source=source,  # note: kwarg can't be named "source", Flask reserves that name
        is_prediction=is_pred,
        recent=recent,
        photos=photos,
    )


@app.route("/location/<int:location_id>/rate", methods=["POST"])
def rate_location(location_id):
    level = int(request.form["level"])
    level = max(1, min(10, level))
    conn = get_db()
    conn.execute(
        "INSERT INTO ratings (location_id, level, timestamp) VALUES (?, ?, ?)",
        (location_id, level, datetime.now().isoformat()),
    )
    conn.commit()
    return redirect(url_for("location_detail", location_id=location_id))


@app.route("/location/<int:location_id>/photo", methods=["POST"])
def upload_photo(location_id):
    conn = get_db()
    loc = conn.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
    if loc is None:
        return "Location not found", 404

    file = request.files.get("photo")
    if file is None or file.filename == "":
        return redirect(url_for("location_detail", location_id=location_id))

    if not allowed_file(file.filename):
        return "Unsupported file type. Please upload a jpg, png, gif, or webp.", 400

    # Prefix with location id + timestamp so filenames never collide, and
    # run the original name through secure_filename to strip anything unsafe.
    timestamp = datetime.now().isoformat()
    safe_name = secure_filename(file.filename)
    stored_name = f"{location_id}_{timestamp.replace(':', '-')}_{safe_name}"
    file.save(os.path.join(UPLOAD_FOLDER, stored_name))

    caption = request.form.get("caption", "").strip()
    conn.execute(
        "INSERT INTO photos (location_id, filename, caption, timestamp) VALUES (?, ?, ?, ?)",
        (location_id, stored_name, caption, timestamp),
    )
    conn.commit()
    return redirect(url_for("location_detail", location_id=location_id))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    """Serves the saved photos so <img> tags in the templates can show them."""
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/api/locations")
def api_locations():
    """JSON feed, handy if you want to build a mobile widget later."""
    conn = get_db()
    locations = conn.execute("SELECT * FROM locations ORDER BY name").fetchall()
    data = []
    for loc in locations:
        level, source, is_pred = get_level_for_location(loc)
        photo_count = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE location_id=?", (loc["id"],)
        ).fetchone()[0]
        data.append(
            {
                "id": loc["id"],
                "name": loc["name"],
                "category": loc["category"],
                "level": level,
                "label": level_to_word(level),
                "is_prediction": is_pred,
                "source": source,
                "photo_count": photo_count,
            }
        )
    return jsonify(data)


@app.route("/api/location/<int:location_id>/photos")
def api_location_photos(location_id):
    """JSON feed of every photo tagged to a location, newest first."""
    conn = get_db()
    photos = conn.execute(
        "SELECT id, filename, caption, timestamp FROM photos WHERE location_id=? "
        "ORDER BY timestamp DESC",
        (location_id,),
    ).fetchall()
    return jsonify(
        [
            {
                "id": p["id"],
                "url": url_for("uploaded_file", filename=p["filename"], _external=True),
                "caption": p["caption"],
                "timestamp": p["timestamp"],
            }
            for p in photos
        ]
    )


# ---------------------------------------------------------------------------
# Templates (kept inline so the whole app is one file)
# ---------------------------------------------------------------------------
BASE_STYLE = """
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f4f5f7; margin:0; padding:24px; color:#222; }
  h1 { margin-bottom:4px; }
  .subtitle { color:#666; margin-top:0; margin-bottom:24px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(240px,1fr)); gap:16px; }
  .card { background:white; border-radius:10px; padding:16px 18px; box-shadow:0 1px 3px rgba(0,0,0,0.08); text-decoration:none; color:inherit; display:block; }
  .card:hover { box-shadow:0 2px 8px rgba(0,0,0,0.15); }
  .badge { display:inline-block; padding:4px 10px; border-radius:999px; color:white; font-weight:600; font-size:14px; }
  .name { font-weight:600; font-size:16px; margin:10px 0 4px; }
  .cat { color:#888; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .source { color:#999; font-size:12px; margin-top:8px; }
  .predicted-tag { font-size:11px; background:#eee; color:#555; padding:2px 8px; border-radius:6px; margin-left:6px; }
  .back { display:inline-block; margin-bottom:16px; color:#555; text-decoration:none; }
  .big-badge { font-size:32px; padding:14px 22px; }
  form.rate { margin-top:20px; background:white; padding:18px; border-radius:10px; box-shadow:0 1px 3px rgba(0,0,0,0.08); max-width:360px; }
  #micBtn { background:#1565c0; margin-bottom:8px; }
  #micBtn:hover { background:#0d47a1; }
  #micBtn:disabled { background:#999; cursor:wait; }
  .mic-status { font-size:12px; color:#666; min-height:16px; margin:4px 0 12px; }
  input[type=range] { width:100%; }
  button { background:#222; color:white; border:none; padding:10px 16px; border-radius:6px; cursor:pointer; font-size:14px; }
  button:hover { background:#444; }
  table { border-collapse:collapse; width:100%; max-width:360px; margin-top:16px; }
  td, th { text-align:left; padding:4px 8px; font-size:13px; border-bottom:1px solid #eee; }
  .photo-count { font-size:11px; color:#777; margin-top:6px; }
  form.photo-upload { margin-top:20px; background:white; padding:18px; border-radius:10px; box-shadow:0 1px 3px rgba(0,0,0,0.08); max-width:360px; }
  form.photo-upload input[type=text] { width:100%; padding:8px; margin:8px 0; border:1px solid #ddd; border-radius:6px; box-sizing:border-box; }
  input[type=file] { display:block; margin:10px 0; }
  .gallery { display:grid; grid-template-columns: repeat(auto-fill, minmax(150px,1fr)); gap:12px; max-width:600px; margin-top:16px; }
  .gallery figure { margin:0; background:white; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.08); }
  .gallery img { width:100%; height:120px; object-fit:cover; display:block; }
  .gallery figcaption { font-size:11px; color:#666; padding:6px 8px; }
</style>
"""

DASHBOARD_HTML = """
<!doctype html><html><head><title>Silence - Campus Quiet Levels</title>""" + BASE_STYLE + """</head>
<body>
  <h1>🤫 Silence</h1>
  <p class="subtitle">Sorted quietest first. Tap a spot to rate it or see details.</p>
  <div class="grid">
  {% for loc in board %}
    <a class="card" href="/location/{{ loc.id }}">
      <span class="badge" style="background:{{ loc.color }}">{{ loc.level }}/10 · {{ loc.word }}</span>
      {% if loc.is_prediction %}<span class="predicted-tag">predicted</span>{% endif %}
      <div class="name">{{ loc.name }}</div>
      <div class="cat">{{ loc.category }}</div>
      <div class="source">{{ loc.source }}</div>
      {% if loc.photo_count %}<div class="photo-count">📷 {{ loc.photo_count }} photo{{ 's' if loc.photo_count != 1 else '' }}</div>{% endif %}
    </a>
  {% endfor %}
  </div>
</body></html>
"""

DETAIL_HTML = """
<!doctype html><html><head><title>{{ loc.name }}</title>""" + BASE_STYLE + """</head>
<body>
  <a class="back" href="/">&larr; Back to all locations</a>
  <h1>{{ loc.name }}</h1>
  <p class="cat">{{ loc.category }}</p>
  <span class="badge big-badge" style="background:{{ color }}">{{ level }}/10 &middot; {{ word }}</span>
  {% if is_prediction %}<p class="source">No recent ratings yet &mdash; this is a {{ level_source }}.</p>
  {% else %}<p class="source">Based on {{ level_source }}.</p>{% endif %}

  <form class="rate" method="POST" action="/location/{{ loc.id }}/rate">
    <label for="level"><strong>Rate the noise here right now (1 = silent, 10 = very loud):</strong></label><br><br>

    <button type="button" id="micBtn" onclick="measureNoise()">🎤 Measure with mic (3s)</button>
    <p id="micStatus" class="mic-status"></p>

    <input type="range" min="1" max="10" value="5" name="level" id="level"
           oninput="document.getElementById('levelval').innerText=this.value">
    <p>Selected: <span id="levelval">5</span>/10</p>
    <button type="submit">Submit rating</button>
  </form>

  <script>
    async function measureNoise() {
      const btn = document.getElementById('micBtn');
      const status = document.getElementById('micStatus');
      const slider = document.getElementById('level');
      const label = document.getElementById('levelval');

      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        status.textContent = "Mic access isn't supported in this browser — use the slider instead.";
        return;
      }

      btn.disabled = true;
      status.textContent = "Listening... hold still for 3 seconds.";

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const source = audioCtx.createMediaStreamSource(stream);
        const analyser = audioCtx.createAnalyser();
        analyser.fftSize = 2048;
        source.connect(analyser);

        const data = new Uint8Array(analyser.fftSize);
        const samples = [];
        const durationMs = 3000;
        const start = Date.now();

        function sample() {
          analyser.getByteTimeDomainData(data);
          // Root-mean-square deviation from the midpoint (128) = amplitude, 0-1
          let sumSquares = 0;
          for (let i = 0; i < data.length; i++) {
            const dev = (data[i] - 128) / 128;
            sumSquares += dev * dev;
          }
          samples.push(Math.sqrt(sumSquares / data.length));

          if (Date.now() - start < durationMs) {
            requestAnimationFrame(sample);
          } else {
            finish();
          }
        }

        function finish() {
          stream.getTracks().forEach(track => track.stop());
          audioCtx.close();

          const avgRms = samples.reduce((a, b) => a + b, 0) / samples.length;
          // Convert amplitude to a rough dB-like scale, then map to 1-10.
          // This is a RELATIVE loudness estimate, not a calibrated dB(SPL) reading —
          // phone mics aren't calibrated instruments, so treat it as "quiet vs loud," not exact.
          const db = 20 * Math.log10(Math.max(avgRms, 0.0001)); // roughly -80 (silent) to 0 (max)
          const normalized = Math.min(Math.max((db + 70) / 70, 0), 1); // -70..0 -> 0..1
          const level = Math.round(1 + normalized * 9);

          slider.value = level;
          label.textContent = level;
          status.textContent = "Measured " + level + "/10 (approximate — feel free to adjust before submitting).";
          btn.disabled = false;
        }

        requestAnimationFrame(sample);
      } catch (err) {
        status.textContent = "Couldn't access the mic (permission denied or unavailable) — use the slider instead.";
        btn.disabled = false;
      }
    }
  </script>

  {% if recent %}
  <table>
    <tr><th>Level</th><th>When</th></tr>
    {% for r in recent %}
    <tr><td>{{ r.level }}</td><td>{{ r.timestamp[:16].replace('T', ' ') }}</td></tr>
    {% endfor %}
  </table>
  {% endif %}

  <form class="photo-upload" method="POST" action="/location/{{ loc.id }}/photo" enctype="multipart/form-data">
    <strong>Add a photo of this spot</strong><br>
    <input type="file" name="photo" accept="image/*" capture="environment" required>
    <input type="text" name="caption" placeholder="Optional caption (e.g. 'packed at lunch')">
    <button type="submit">Upload photo</button>
  </form>

  {% if photos %}
  <h3>Photos from students ({{ photos|length }})</h3>
  <div class="gallery">
    {% for p in photos %}
    <figure>
      <img src="/uploads/{{ p.filename }}" alt="Photo of {{ loc.name }}">
      <figcaption>
        {% if p.caption %}{{ p.caption }}<br>{% endif %}
        {{ p.timestamp[:16].replace('T', ' ') }}
      </figcaption>
    </figure>
    {% endfor %}
  </div>
  {% endif %}
</body></html>
"""

init_db()  # ensure tables exist whether run directly or under a WSGI server

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
