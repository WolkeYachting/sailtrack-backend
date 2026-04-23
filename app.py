"""
SailTrack Backend – Phase 2a

Neu gegenüber Phase 1:
- MultiLineString + sessions-Array statt flacher LineString
- Session-Management (start, points, end)
- Track-Status-Toggle (active/finished, beliebig reversibel)
- Metadaten editieren (PATCH)
- Backend filtert Punkte nach manuellem ended_at

Weiterhin:
- Sofort-Commit bei jedem Write (keine 10-Min-Batches, kommt Phase 3)
- Keine Live-Endpoints (auch Phase 3)
"""

import base64
import hashlib
import json
import math
import os
import secrets
import time
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ============================================================================
# Config
# ============================================================================

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "wolkeyachting")
DATA_REPO    = os.environ.get("DATA_REPO", "sailtrack")
ADMIN_REPO   = os.environ.get("ADMIN_REPO", "sailtrack-admin")
ADMIN_TOKEN  = os.environ.get("SAILTRACK_ADMIN_TOKEN", "")

SKIPPER_TOKENS_RAW = os.environ.get("SAILTRACK_TOKENS", "")
SKIPPER_TOKENS = {
    kv.split("=", 1)[0].strip(): kv.split("=", 1)[1].strip()
    for kv in SKIPPER_TOKENS_RAW.split(",")
    if "=" in kv
}

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH   = 12

# ============================================================================
# Helpers
# ============================================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def generate_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))

def hash_code(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()

def auth_skipper():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    name = SKIPPER_TOKENS.get(token)
    return (name, token) if name else None

def auth_admin():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return ADMIN_TOKEN and auth[7:] == ADMIN_TOKEN

def haversine_nm(lat1, lon1, lat2, lon2):
    R_nm = 3440.065
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(df/2)**2 + math.cos(f1) * math.cos(f2) * math.sin(dl/2)**2
    return 2 * R_nm * math.asin(math.sqrt(a))

def iso_to_ts(iso_str):
    """ISO 8601 -> Unix timestamp (int)."""
    if iso_str is None:
        return None
    iso_str = iso_str.replace("Z", "+00:00")
    return int(datetime.fromisoformat(iso_str).timestamp())

# ============================================================================
# GitHub API
# ============================================================================

GH_API = "https://api.github.com"

def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def gh_get_file(repo, path):
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]

def gh_put_file(repo, path, content_dict, message, sha=None):
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/contents/{path}"
    body = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(content_dict, indent=2, ensure_ascii=False).encode("utf-8")
        ).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=gh_headers(), json=body, timeout=30)
    r.raise_for_status()
    return r.json()

def gh_update_file_retrying(repo, path, updater_fn, message):
    for attempt in range(3):
        existing, sha = gh_get_file(repo, path)
        new_content = updater_fn(existing)
        try:
            gh_put_file(repo, path, new_content, message, sha=sha)
            return new_content
        except requests.HTTPError as e:
            if e.response.status_code == 409 and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise

# ============================================================================
# Admin-Index
# ============================================================================

def admin_index_upsert(entry):
    """Mergt Entry-Felder in existierenden Eintrag, oder legt neu an."""
    def update(existing):
        data = existing or {"tracks": []}
        found = False
        for i, t in enumerate(data["tracks"]):
            if t["id"] == entry["id"]:
                data["tracks"][i] = {**t, **entry}
                found = True
                break
        if not found:
            data["tracks"].append(entry)
        return data
    gh_update_file_retrying(
        ADMIN_REPO, "index.json", update, f"admin: upsert {entry['id']}"
    )

# ============================================================================
# Track-Struktur Helpers
# ============================================================================

def empty_track(track_id, name, boat, skipper, trip_start=None, trip_end=None):
    """Neuer Track im MultiLineString-Format (Phase 2a)."""
    return {
        "type": "FeatureCollection",
        "properties": {
            "id": track_id,
            "name": name,
            "boat": boat,
            "skipper": skipper,
            "trip_start": trip_start,
            "trip_end": trip_end,
            "status": "active",
            "created_at": now_iso(),
            "stats": {
                "distance_total_nm": 0.0,
                "max_sog": 0.0,
                "point_count": 0,
                "session_count": 0,
            },
        },
        "features": [{
            "type": "Feature",
            "geometry": {"type": "MultiLineString", "coordinates": []},
            "properties": {"sessions": []},
        }],
    }

def recalc_stats(track):
    """Stats aus allen Sessions neu berechnen."""
    feat = track["features"][0]
    coords_all = feat["geometry"]["coordinates"]
    sessions = feat["properties"]["sessions"]
    
    total_nm = 0.0
    max_sog = 0.0
    point_count = 0
    
    for i, sess_coords in enumerate(coords_all):
        for j in range(1, len(sess_coords)):
            lon1, lat1 = sess_coords[j-1]
            lon2, lat2 = sess_coords[j]
            total_nm += haversine_nm(lat1, lon1, lat2, lon2)
        point_count += len(sess_coords)
        if i < len(sessions):
            sogs = sessions[i].get("sog", [])
            if sogs:
                max_sog = max(max_sog, max(sogs))
    
    track["properties"]["stats"] = {
        "distance_total_nm": round(total_nm, 3),
        "max_sog": round(max_sog, 2),
        "point_count": point_count,
        "session_count": len(sessions),
    }

# ============================================================================
# Endpoints – Track
# ============================================================================

@app.route("/")
def root():
    return jsonify({"service": "sailtrack-backend", "status": "ok", "phase": "2a"})

@app.route("/tracks", methods=["POST"])
def create_track():
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    
    body = request.get_json(force=True, silent=True) or {}
    name = body.get("name", "").strip() or "Unbenannter Track"
    boat = body.get("boat", "").strip() or "–"
    trip_start = body.get("trip_start")
    trip_end = body.get("trip_end")
    
    code = generate_code()
    file_hash = hash_code(code)
    track_id = code
    
    track = empty_track(track_id, name, boat, skipper_name, trip_start, trip_end)
    
    gh_put_file(
        DATA_REPO, f"tracks/{file_hash}.geojson",
        track, f"create track {file_hash[:8]}"
    )
    
    admin_index_upsert({
        "id": track_id,
        "code": code,
        "file_hash": file_hash,
        "name": name,
        "boat": boat,
        "skipper": skipper_name,
        "trip_start": trip_start,
        "trip_end": trip_end,
        "status": "active",
        "created_at": track["properties"]["created_at"],
    })
    
    return jsonify({"id": track_id, "code": code, "file_hash": file_hash})

@app.route("/tracks/<track_id>", methods=["PATCH"])
def patch_track(track_id):
    """Metadaten editieren (name, boat, trip_start, trip_end)."""
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    
    body = request.get_json(force=True, silent=True) or {}
    allowed = {"name", "boat", "trip_start", "trip_end"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return jsonify({"error": "no valid fields"}), 400
    
    file_hash = hash_code(track_id)
    
    def update(existing):
        if existing is None:
            raise ValueError("track not found")
        for k, v in updates.items():
            existing["properties"][k] = v
        return existing
    
    try:
        gh_update_file_retrying(
            DATA_REPO, f"tracks/{file_hash}.geojson", update,
            f"patch {file_hash[:8]}: {','.join(updates.keys())}"
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    
    admin_updates = {"id": track_id, **updates}
    admin_index_upsert(admin_updates)
    
    return jsonify({"id": track_id, **updates})

# ============================================================================
# Endpoints – Sessions
# ============================================================================

@app.route("/tracks/<track_id>/sessions", methods=["POST"])
def start_session(track_id):
    """Startet neue Session. Returns session_index."""
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    
    body = request.get_json(force=True, silent=True) or {}
    started_at = body.get("started_at") or now_iso()
    
    file_hash = hash_code(track_id)
    session_index_ref = {"value": -1}
    
    def update(existing):
        if existing is None:
            raise ValueError("track not found")
        feat = existing["features"][0]
        new_session = {
            "started_at": started_at,
            "ended_at": None,
            "timestamps": [],
            "sog": [],
            "cog": [],
        }
        feat["properties"]["sessions"].append(new_session)
        feat["geometry"]["coordinates"].append([])
        session_index_ref["value"] = len(feat["properties"]["sessions"]) - 1
        recalc_stats(existing)
        return existing
    
    try:
        gh_update_file_retrying(
            DATA_REPO, f"tracks/{file_hash}.geojson", update,
            f"session start {file_hash[:8]}#{session_index_ref['value']}"
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    
    return jsonify({"session_index": session_index_ref["value"]})

@app.route("/tracks/<track_id>/sessions/<int:sess_idx>/end", methods=["POST"])
def end_session(track_id, sess_idx):
    """Beendet Session, filtert Punkte mit timestamp > ended_at."""
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    
    body = request.get_json(force=True, silent=True) or {}
    ended_at = body.get("ended_at") or now_iso()
    ended_at_ts = iso_to_ts(ended_at)
    
    file_hash = hash_code(track_id)
    
    def update(existing):
        if existing is None:
            raise ValueError("track not found")
        feat = existing["features"][0]
        sessions = feat["properties"]["sessions"]
        coords = feat["geometry"]["coordinates"]
        
        if sess_idx < 0 or sess_idx >= len(sessions):
            raise ValueError(f"session {sess_idx} not found")
        
        sess = sessions[sess_idx]
        sess_coords = coords[sess_idx]
        timestamps = sess["timestamps"]
        sogs = sess["sog"]
        cogs = sess["cog"]
        
        new_ts, new_coords, new_sogs, new_cogs = [], [], [], []
        for i, t in enumerate(timestamps):
            if t <= ended_at_ts:
                new_ts.append(t)
                new_coords.append(sess_coords[i])
                new_sogs.append(sogs[i])
                new_cogs.append(cogs[i])
        
        sess["timestamps"] = new_ts
        sess["sog"] = new_sogs
        sess["cog"] = new_cogs
        sess["ended_at"] = ended_at
        coords[sess_idx] = new_coords
        
        recalc_stats(existing)
        return existing
    
    try:
        gh_update_file_retrying(
            DATA_REPO, f"tracks/{file_hash}.geojson", update,
            f"session end {file_hash[:8]}#{sess_idx}"
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    
    return jsonify({"session_index": sess_idx, "ended_at": ended_at})

@app.route("/tracks/<track_id>/points", methods=["POST"])
def append_points(track_id):
    """
    Body: { "session_index": N, "points": [{"t":..., "lat":..., "lon":..., "sog":..., "cog":...}] }
    """
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    
    body = request.get_json(force=True, silent=True) or {}
    sess_idx = body.get("session_index")
    points = body.get("points", [])
    
    if sess_idx is None:
        return jsonify({"error": "session_index required"}), 400
    if not points:
        return jsonify({"accepted": 0})
    
    file_hash = hash_code(track_id)
    
    def update(existing):
        if existing is None:
            raise ValueError("track not found")
        feat = existing["features"][0]
        sessions = feat["properties"]["sessions"]
        coords = feat["geometry"]["coordinates"]
        
        if sess_idx < 0 or sess_idx >= len(sessions):
            raise ValueError(f"session {sess_idx} not found")
        
        sess = sessions[sess_idx]
        if sess["ended_at"] is not None:
            raise ValueError(f"session {sess_idx} already ended")
        
        for p in points:
            coords[sess_idx].append([p["lon"], p["lat"]])
            sess["timestamps"].append(p["t"])
            sess["sog"].append(p.get("sog", 0.0))
            sess["cog"].append(p.get("cog", 0.0))
        
        recalc_stats(existing)
        return existing
    
    try:
        gh_update_file_retrying(
            DATA_REPO, f"tracks/{file_hash}.geojson", update,
            f"append {len(points)} pts to {file_hash[:8]}#{sess_idx}"
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    
    return jsonify({"accepted": len(points)})

# ============================================================================
# Endpoints – Track-Status-Toggle
# ============================================================================

def _set_status(track_id, new_status):
    file_hash = hash_code(track_id)
    
    def update(existing):
        if existing is None:
            raise ValueError("track not found")
        existing["properties"]["status"] = new_status
        if new_status == "finished":
            existing["properties"]["finished_at"] = now_iso()
        else:
            existing["properties"].pop("finished_at", None)
        return existing
    
    updated = gh_update_file_retrying(
        DATA_REPO, f"tracks/{file_hash}.geojson", update,
        f"status->{new_status} {file_hash[:8]}"
    )
    
    admin_index_upsert({"id": track_id, "status": new_status})
    return updated

@app.route("/tracks/<track_id>/finish", methods=["POST"])
def finish_track(track_id):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    try:
        updated = _set_status(track_id, "finished")
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"status": "finished", "stats": updated["properties"]["stats"]})

@app.route("/tracks/<track_id>/reopen", methods=["POST"])
def reopen_track(track_id):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    try:
        _set_status(track_id, "active")
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"status": "active"})

# ============================================================================
# Endpoints – Listings
# ============================================================================

@app.route("/tracks", methods=["GET"])
def list_my_tracks():
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    
    existing, _ = gh_get_file(ADMIN_REPO, "index.json")
    if not existing:
        return jsonify([])
    mine = [
        {k: v for k, v in t.items() if k != "code"}
        for t in existing["tracks"]
        if t.get("skipper") == skipper_name
    ]
    return jsonify(mine)

@app.route("/tracks/<track_id>", methods=["GET"])
def get_track(track_id):
    """Volle Track-Daten (für App zum Wiederaufnehmen)."""
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    
    file_hash = hash_code(track_id)
    existing, _ = gh_get_file(DATA_REPO, f"tracks/{file_hash}.geojson")
    if not existing:
        return jsonify({"error": "track not found"}), 404
    return jsonify(existing)

@app.route("/admin/index", methods=["GET"])
def admin_index():
    if not auth_admin():
        return jsonify({"error": "unauthorized"}), 401
    existing, _ = gh_get_file(ADMIN_REPO, "index.json")
    return jsonify(existing or {"tracks": []})

# ============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
