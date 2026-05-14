"""
SailTrack Backend – v2 Datenmodell (Phase 2c)

Key changes:
- Track ist LineString (statt MultiLineString)
- Pausen werden separat unter properties.pauses gespeichert
- Kein session_index mehr, weder bei Punkten noch in Datei-Struktur
- POST /tracks/{id}/points: nur {points: [{t, lat, lon}]}
- POST /tracks/{id}/pauses: nur {t: timestamp}
- Server akzeptiert Punkte unabhängig vom Track-Status
- Out-of-order Punkte werden chronologisch einsortiert
"""

import base64
import bisect
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

LEGACY_TOKENS_RAW = os.environ.get("SAILTRACK_TOKENS", "")
LEGACY_TOKENS = {
    kv.split("=", 1)[0].strip(): kv.split("=", 1)[1].strip()
    for kv in LEGACY_TOKENS_RAW.split(",")
    if "=" in kv
}

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH   = 12

LAST_SEEN_THROTTLE_SEC = 300
_last_seen_debounce = {}

# ============================================================================
# Helpers
# ============================================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def generate_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))

def generate_skipper_token():
    return secrets.token_urlsafe(32)

def hash_code(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()

def haversine_nm(lat1, lon1, lat2, lon2):
    R_nm = 3440.065
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(df/2)**2 + math.cos(f1) * math.cos(f2) * math.sin(dl/2)**2
    return 2 * R_nm * math.asin(math.sqrt(a))

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
# Skipper-Register
# ============================================================================

def _load_skippers():
    existing, _ = gh_get_file(ADMIN_REPO, "skippers.json")
    return existing or {"skippers": []}

def _find_skipper(skippers_data, token):
    for s in skippers_data.get("skippers", []):
        if s.get("token") == token:
            return s
    return None

def auth_skipper():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    if token in LEGACY_TOKENS:
        return (LEGACY_TOKENS[token], token)
    try:
        skippers_data = _load_skippers()
    except Exception:
        return None
    found = _find_skipper(skippers_data, token)
    if found:
        _maybe_update_last_seen(token)
        return (found.get("name", "Unbekannt"), token)
    return None

def auth_admin():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    return ADMIN_TOKEN and auth_header[7:] == ADMIN_TOKEN

def _maybe_update_last_seen(token):
    now = time.time()
    last = _last_seen_debounce.get(token, 0)
    if now - last < LAST_SEEN_THROTTLE_SEC:
        return
    _last_seen_debounce[token] = now
    def update(existing):
        data = existing or {"skippers": []}
        for s in data.get("skippers", []):
            if s.get("token") == token:
                s["last_seen_at"] = now_iso()
                break
        return data
    try:
        gh_update_file_retrying(
            ADMIN_REPO, "skippers.json", update, f"last_seen: {token[:8]}..."
        )
    except Exception:
        pass

# ============================================================================
# Skipper-Endpoints
# ============================================================================

@app.route("/skippers/new", methods=["POST"])
def new_skipper():
    token = generate_skipper_token()
    def update(existing):
        data = existing or {"skippers": []}
        if any(s.get("token") == token for s in data["skippers"]):
            raise ValueError("collision")
        default_name = f"Skipper-{token[:6]}"
        data["skippers"].append({
            "token": token,
            "name": default_name,
            "created_at": now_iso(),
            "last_seen_at": now_iso(),
        })
        return data
    gh_update_file_retrying(
        ADMIN_REPO, "skippers.json", update, f"skipper new: {token[:8]}..."
    )
    return jsonify({"token": token, "name": f"Skipper-{token[:6]}"})

@app.route("/whoami", methods=["GET"])
def whoami():
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    name, _ = skipper
    return jsonify({"name": name})

# ============================================================================
# Admin
# ============================================================================

@app.route("/admin/skippers", methods=["GET"])
def admin_list_skippers():
    if not auth_admin():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_load_skippers())

@app.route("/admin/skippers/<token>", methods=["PATCH"])
def admin_patch_skipper(token):
    if not auth_admin():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(force=True, silent=True) or {}
    new_name = body.get("name", "").strip()
    if not new_name:
        return jsonify({"error": "name required"}), 400
    def update(existing):
        data = existing or {"skippers": []}
        for s in data.get("skippers", []):
            if s.get("token") == token:
                s["name"] = new_name
                return data
        raise ValueError("skipper not found")
    try:
        gh_update_file_retrying(
            ADMIN_REPO, "skippers.json", update, f"admin: rename {token[:8]}"
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"token": token, "name": new_name})

def admin_index_upsert(entry):
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
# Track-Struktur (v2: flat LineString)
# ============================================================================

def empty_track(track_id, name, boat, skipper, trip_start=None, trip_end=None):
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
            "pauses": [],
            "stats": {
                "distance_total_nm": 0.0,
                "point_count": 0,
            },
        },
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": []},
            "properties": {"timestamps": []},
        }],
    }

def recalc_stats(track):
    """Distanz neu berechnen über alle Punkte, mit Sprüngen über Pausen."""
    feat = track["features"][0]
    coords = feat["geometry"]["coordinates"]
    timestamps = feat["properties"]["timestamps"]
    pauses = track["properties"].get("pauses", [])
    pauses_sorted = sorted(pauses)

    total_nm = 0.0
    for j in range(1, len(coords)):
        # Distanz zum vorigen Punkt zählt nur, wenn dazwischen KEINE Pause liegt
        t_prev = timestamps[j-1]
        t_cur = timestamps[j]
        # Pause zwischen t_prev und t_cur?
        idx = bisect.bisect_left(pauses_sorted, t_prev)
        crossed_pause = idx < len(pauses_sorted) and pauses_sorted[idx] < t_cur
        if not crossed_pause:
            lon1, lat1 = coords[j-1]
            lon2, lat2 = coords[j]
            total_nm += haversine_nm(lat1, lon1, lat2, lon2)

    track["properties"]["stats"] = {
        "distance_total_nm": round(total_nm, 3),
        "point_count": len(coords),
    }

def load_track_and_check_ownership(track_id, skipper_name):
    file_hash = hash_code(track_id)
    track, _ = gh_get_file(DATA_REPO, f"tracks/{file_hash}.geojson")
    if track is None:
        raise LookupError("track not found")
    if track["properties"].get("skipper") != skipper_name:
        raise PermissionError("not owner")
    return track, file_hash

# ============================================================================
# Track-Endpoints
# ============================================================================

@app.route("/")
def root():
    return jsonify({
        "service": "sailtrack-backend",
        "status": "ok",
        "phase": "2c",
    })

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
        "id": track_id, "code": code, "file_hash": file_hash,
        "name": name, "boat": boat, "skipper": skipper_name,
        "trip_start": trip_start, "trip_end": trip_end,
        "status": "active", "created_at": track["properties"]["created_at"],
    })
    return jsonify({"id": track_id, "code": code, "file_hash": file_hash})

@app.route("/tracks/<track_id>", methods=["PATCH"])
def patch_track(track_id):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    body = request.get_json(force=True, silent=True) or {}
    allowed = {"name", "boat", "trip_start", "trip_end"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return jsonify({"error": "no valid fields"}), 400
    file_hash = hash_code(track_id)
    def update(existing):
        if existing is None:
            raise LookupError("track not found")
        if existing["properties"].get("skipper") != skipper_name:
            raise PermissionError("not owner")
        for k, v in updates.items():
            existing["properties"][k] = v
        return existing
    try:
        gh_update_file_retrying(
            DATA_REPO, f"tracks/{file_hash}.geojson", update,
            f"patch {file_hash[:8]}: {','.join(updates.keys())}"
        )
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    admin_index_upsert({"id": track_id, **updates})
    return jsonify({"id": track_id, **updates})

# ============================================================================
# Points (v2: ohne session_index, chronologisch sortiert)
# ============================================================================

@app.route("/tracks/<track_id>/points", methods=["POST"])
def append_points(track_id):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper

    body = request.get_json(force=True, silent=True) or {}
    points = body.get("points", [])
    if not points:
        return jsonify({"accepted": 0})

    # Punkte vorsortieren nach Zeit, damit der Server-Code simpel bleibt
    points = sorted(points, key=lambda p: p["t"])

    file_hash = hash_code(track_id)

    def update(existing):
        if existing is None:
            raise LookupError("track not found")
        if existing["properties"].get("skipper") != skipper_name:
            raise PermissionError("not owner")
        feat = existing["features"][0]
        coords = feat["geometry"]["coordinates"]
        timestamps = feat["properties"]["timestamps"]

        for p in points:
            t, lat, lon = p["t"], p["lat"], p["lon"]
            # Üblicher Fall: chronologisch hinten anhängen
            if not timestamps or t >= timestamps[-1]:
                timestamps.append(t)
                coords.append([lon, lat])
            else:
                # Out-of-order: an die korrekte Stelle einsortieren
                idx = bisect.bisect_left(timestamps, t)
                timestamps.insert(idx, t)
                coords.insert(idx, [lon, lat])

        recalc_stats(existing)
        return existing

    try:
        gh_update_file_retrying(
            DATA_REPO, f"tracks/{file_hash}.geojson", update,
            f"append {len(points)} pts to {file_hash[:8]}"
        )
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    return jsonify({"accepted": len(points)})

@app.route("/tracks/<track_id>/pauses", methods=["POST"])
def add_pause(track_id):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper

    body = request.get_json(force=True, silent=True) or {}
    t = body.get("t")
    if t is None:
        return jsonify({"error": "t required"}), 400

    file_hash = hash_code(track_id)

    def update(existing):
        if existing is None:
            raise LookupError("track not found")
        if existing["properties"].get("skipper") != skipper_name:
            raise PermissionError("not owner")
        pauses = existing["properties"].setdefault("pauses", [])
        if t not in pauses:
            pauses.append(t)
            pauses.sort()
        recalc_stats(existing)
        return existing

    try:
        gh_update_file_retrying(
            DATA_REPO, f"tracks/{file_hash}.geojson", update,
            f"pause @ {t} on {file_hash[:8]}"
        )
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    return jsonify({"t": t})

# ============================================================================
# Status-Toggle
# ============================================================================

def _set_status(track_id, new_status, skipper_name):
    file_hash = hash_code(track_id)
    def update(existing):
        if existing is None:
            raise LookupError("track not found")
        if existing["properties"].get("skipper") != skipper_name:
            raise PermissionError("not owner")
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
    skipper_name, _ = skipper
    try:
        updated = _set_status(track_id, "finished", skipper_name)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    return jsonify({"status": "finished", "stats": updated["properties"]["stats"]})

@app.route("/tracks/<track_id>/reopen", methods=["POST"])
def reopen_track(track_id):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    try:
        updated = _set_status(track_id, "active", skipper_name)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    return jsonify({"status": "active", "stats": updated["properties"]["stats"]})

# ============================================================================
# Listings
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
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    file_hash = hash_code(track_id)
    existing, _ = gh_get_file(DATA_REPO, f"tracks/{file_hash}.geojson")
    if not existing:
        return jsonify({"error": "track not found"}), 404
    if existing["properties"].get("skipper") != skipper_name:
        return jsonify({"error": "not owner"}), 403
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
