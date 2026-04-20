"""
SailTrack Backend – Phase 1

Minimaler End-to-End-Stack:
- POST /tracks                 Track erstellen (erzeugt Code + leere GeoJSON)
- POST /tracks/{id}/points     Punkte anhängen
- POST /tracks/{id}/finish     Track abschließen + Stats
- GET  /tracks                 Eigene Tracks (nach Skipper-Token)
- GET  /admin/index            Admin-Index mit Codes (nur mit Admin-Token)

Phase-1-Vereinfachungen:
- Keine Sessions (flacher LineString statt MultiLineString)
- Kein Live-Endpoint
- Sofort-Commit bei jedem Write (keine 10-Min-Batches)
- Kein Recovery aus Memory (Render-Schlaf unkritisch, wir lesen state immer aus Repo)
"""

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
CORS(app)  # Website auf github.io darf das Backend anfragen

# ============================================================================
# Config (Env-Vars)
# ============================================================================

GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER   = os.environ.get("GITHUB_OWNER", "wolkeyachting")
DATA_REPO      = os.environ.get("DATA_REPO", "sailtrack")
ADMIN_REPO     = os.environ.get("ADMIN_REPO", "sailtrack-admin")
ADMIN_TOKEN    = os.environ.get("SAILTRACK_ADMIN_TOKEN", "")

# Skipper-Tokens: Format "token1=David,token2=Julian"
SKIPPER_TOKENS_RAW = os.environ.get("SAILTRACK_TOKENS", "")
SKIPPER_TOKENS = {
    kv.split("=", 1)[0].strip(): kv.split("=", 1)[1].strip()
    for kv in SKIPPER_TOKENS_RAW.split(",")
    if "=" in kv
}

# Base32 ohne mehrdeutige Zeichen (0/O, 1/I/L)
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
    """Gibt (skipper_name, token) zurück oder None."""
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
    """Distanz in nautischen Meilen."""
    R_nm = 3440.065  # Erdradius in nm
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ/2)**2
    return 2 * R_nm * math.asin(math.sqrt(a))

# ============================================================================
# GitHub API Wrapper
# ============================================================================

GH_API = "https://api.github.com"

def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def gh_get_file(repo, path):
    """Returns (content_dict, sha) oder (None, None) wenn Datei fehlt."""
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    import base64
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]

def gh_put_file(repo, path, content_dict, message, sha=None):
    """Create oder update. sha=None → create, sha=<existing> → update."""
    import base64
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
    """
    Liest Datei, wendet updater_fn(content) an, schreibt zurück.
    Bei 409 (sha conflict) max. 3x erneut versuchen.
    updater_fn bekommt None bei nicht-existenter Datei.
    """
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
# Admin-Index Pflege
# ============================================================================

def admin_index_upsert(entry):
    """Trägt oder aktualisiert einen Track im Admin-Index."""
    def update(existing):
        data = existing or {"tracks": []}
        # Nach id ersetzen oder anhängen
        found = False
        for i, t in enumerate(data["tracks"]):
            if t["id"] == entry["id"]:
                data["tracks"][i] = entry
                found = True
                break
        if not found:
            data["tracks"].append(entry)
        return data
    gh_update_file_retrying(
        ADMIN_REPO, "index.json", update,
        f"admin: upsert {entry['id']}"
    )

# ============================================================================
# Endpoints
# ============================================================================

@app.route("/")
def root():
    return jsonify({"service": "sailtrack-backend", "status": "ok"})

@app.route("/tracks", methods=["POST"])
def create_track():
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper

    body = request.get_json(force=True, silent=True) or {}
    name = body.get("name", "").strip() or "Unbenannter Track"
    boat = body.get("boat", "").strip() or "–"

    code = generate_code()
    file_hash = hash_code(code)
    track_id = code  # id und code sind aktuell identisch (siehe SPEC)

    # GeoJSON anlegen (flacher LineString für Phase 1)
    track_data = {
        "type": "FeatureCollection",
        "properties": {
            "id": track_id,
            "name": name,
            "boat": boat,
            "skipper": skipper_name,
            "trip_start": body.get("trip_start"),
            "trip_end": body.get("trip_end"),
            "status": "active",
            "created_at": now_iso(),
            "stats": {"distance_total_nm": 0.0, "max_sog": 0.0, "point_count": 0},
        },
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": []},
            "properties": {"timestamps": [], "sog": [], "cog": []},
        }],
    }

    # Datei ins Daten-Repo (öffentlich, per Hash benannt)
    gh_put_file(
        DATA_REPO, f"tracks/{file_hash}.geojson",
        track_data, f"create track {file_hash[:8]}"
    )

    # Admin-Index aktualisieren
    admin_index_upsert({
        "id": track_id,
        "code": code,
        "file_hash": file_hash,
        "name": name,
        "boat": boat,
        "skipper": skipper_name,
        "trip_start": body.get("trip_start"),
        "trip_end": body.get("trip_end"),
        "status": "active",
        "created_at": track_data["properties"]["created_at"],
    })

    return jsonify({"id": track_id, "code": code, "file_hash": file_hash})

@app.route("/tracks/<track_id>/points", methods=["POST"])
def append_points(track_id):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    points = body.get("points", [])
    if not points:
        return jsonify({"accepted": 0})

    file_hash = hash_code(track_id)

    def update(existing):
        if existing is None:
            raise ValueError("track not found")
        if existing["properties"]["status"] != "active":
            raise ValueError("track not active")

        feat = existing["features"][0]
        coords = feat["geometry"]["coordinates"]
        ts = feat["properties"]["timestamps"]
        sog = feat["properties"]["sog"]
        cog = feat["properties"]["cog"]

        for p in points:
            coords.append([p["lon"], p["lat"]])
            ts.append(p["t"])
            sog.append(p.get("sog", 0.0))
            cog.append(p.get("cog", 0.0))

        # Stats live updaten
        props = existing["properties"]
        if len(coords) >= 2:
            last_two = coords[-2:]
            props["stats"]["distance_total_nm"] = round(
                props["stats"]["distance_total_nm"]
                + haversine_nm(last_two[0][1], last_two[0][0],
                               last_two[1][1], last_two[1][0]),
                3,
            )
            # Bei mehreren neuen Punkten noch einmal nachziehen
            for i in range(max(0, len(coords) - len(points) - 1), len(coords) - 2):
                props["stats"]["distance_total_nm"] = round(
                    props["stats"]["distance_total_nm"]
                    + haversine_nm(coords[i][1], coords[i][0],
                                   coords[i+1][1], coords[i+1][0]),
                    3,
                )

        if sog:
            props["stats"]["max_sog"] = round(max(props["stats"]["max_sog"], max(sog[-len(points):])), 2)
        props["stats"]["point_count"] = len(coords)
        return existing

    try:
        gh_update_file_retrying(
            DATA_REPO, f"tracks/{file_hash}.geojson", update,
            f"append {len(points)} pts to {file_hash[:8]}"
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    return jsonify({"accepted": len(points)})

@app.route("/tracks/<track_id>/finish", methods=["POST"])
def finish_track(track_id):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401

    file_hash = hash_code(track_id)

    def update(existing):
        if existing is None:
            raise ValueError("track not found")
        existing["properties"]["status"] = "finished"
        existing["properties"]["finished_at"] = now_iso()
        return existing

    try:
        updated = gh_update_file_retrying(
            DATA_REPO, f"tracks/{file_hash}.geojson", update,
            f"finish track {file_hash[:8]}"
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    # Admin-Index: status nachziehen
    def admin_update(existing):
        if existing is None:
            return {"tracks": []}
        for t in existing["tracks"]:
            if t["id"] == track_id:
                t["status"] = "finished"
        return existing
    gh_update_file_retrying(
        ADMIN_REPO, "index.json", admin_update,
        f"admin: finish {track_id}"
    )

    return jsonify({"stats": updated["properties"]["stats"]})

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
        {k: v for k, v in t.items() if k != "code"}  # Codes nicht im Listing
        for t in existing["tracks"]
        if t.get("skipper") == skipper_name
    ]
    return jsonify(mine)

@app.route("/admin/index", methods=["GET"])
def admin_index():
    if not auth_admin():
        return jsonify({"error": "unauthorized"}), 401
    existing, _ = gh_get_file(ADMIN_REPO, "index.json")
    return jsonify(existing or {"tracks": []})

# ============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
