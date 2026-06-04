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

def ts_hash(timestamps):
    """
    Fingerabdruck ueber die SORTIERTE Timestamp-Folge eines Tracks.

    Da der Timestamp die Punkt-Identitaet ist (Dedup-Schluessel), bedeutet
    gleicher Hash beidseitig: identische Punktmenge. Wird vom App-Client vor
    einem "Daten vom Geraet entfernen" verglichen -- nur bei Gleichheit wird
    lokal geloescht.

    WICHTIG: Der Algorithmus (FNV-1a, 64 Bit, ueber die mit "," verbundenen
    Dezimal-Strings der sortierten Ints) MUSS bitgenau zur Dart-Seite passen
    (fnv1a64Hex in store.dart). Aenderungen hier ohne dort = stiller Bruch.
    """
    s = ",".join(str(t) for t in sorted(timestamps))
    h = 0xcbf29ce484222325
    for b in s.encode("utf-8"):
        h = ((h ^ b) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return format(h, "016x")

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
    """
    Liest eine JSON-Datei aus dem Repo.

    Die Contents-API liefert `content` nur inline, solange die Datei < 1 MB ist.
    Ab 1 MB ist `content` leer und `encoding` == "none" -> der eigentliche
    Inhalt muss ueber die Git Blob API (per `git_url`) nachgeladen werden.
    Die Blob-API hat kein 1-MB-Limit (bis 100 MB).

    Rueckgabe: (parsed_dict_oder_None, blob_sha_oder_None)
    Der zurueckgegebene SHA ist der Blob-SHA der Datei und wird von
    gh_put_file fuer den Tree-Diff bzw. die Existenzpruefung verwendet.
    """
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()

    if data.get("encoding") == "base64" and data.get("content"):
        # Kleine Datei: Inhalt kommt inline mit
        raw = base64.b64decode(data["content"])
    else:
        # Grosse Datei (>= 1 MB): content ist leer -> Blob separat holen
        blob = requests.get(data["git_url"], headers=gh_headers(), timeout=30)
        blob.raise_for_status()
        raw = base64.b64decode(blob.json()["content"])

    text = raw.decode("utf-8")
    if not text.strip():
        # Leere oder kaputte Datei nicht als JSON parsen
        return None, data["sha"]
    return json.loads(text), data["sha"]

def _gh_default_branch(repo):
    r = requests.get(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}",
        headers=gh_headers(), timeout=30,
    )
    r.raise_for_status()
    return r.json()["default_branch"]


def gh_put_file(repo, path, content_dict, message, sha=None):
    """
    Schreibt eine JSON-Datei ueber die Git Data API.

    Die Contents-API (PUT /contents) akzeptiert nur Dateien < 1 MB. Die
    Track-GeoJSONs haben dieses Limit erreicht, daher wird hier der
    Blob/Tree/Commit/Ref-Weg genutzt, der bis 100 MB traegt. Funktioniert
    fuer kleine Dateien (skippers.json, index.json) ebenso.

    Der `sha`-Parameter wird fuer Optimistic-Concurrency nicht mehr
    gebraucht (das uebernimmt das Ref-Update), bleibt aber in der Signatur,
    damit bestehende Aufrufer unveraendert funktionieren.
    """
    branch = _gh_default_branch(repo)

    # Aktuellen Commit der Branch-Spitze ermitteln
    ref_url = f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/ref/heads/{branch}"
    ref = requests.get(ref_url, headers=gh_headers(), timeout=30)
    ref.raise_for_status()
    base_commit_sha = ref.json()["object"]["sha"]

    commit = requests.get(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/commits/{base_commit_sha}",
        headers=gh_headers(), timeout=30,
    )
    commit.raise_for_status()
    base_tree_sha = commit.json()["tree"]["sha"]

    payload = json.dumps(content_dict, indent=2, ensure_ascii=False)

    # 1) Blob anlegen
    blob = requests.post(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/blobs",
        headers=gh_headers(), timeout=30,
        json={
            "content": base64.b64encode(payload.encode("utf-8")).decode("ascii"),
            "encoding": "base64",
        },
    )
    blob.raise_for_status()
    blob_sha = blob.json()["sha"]

    # 2) Tree mit der geaenderten Datei erzeugen
    tree = requests.post(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/trees",
        headers=gh_headers(), timeout=30,
        json={
            "base_tree": base_tree_sha,
            "tree": [{
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            }],
        },
    )
    tree.raise_for_status()
    new_tree_sha = tree.json()["sha"]

    # 3) Commit erzeugen
    new_commit = requests.post(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/commits",
        headers=gh_headers(), timeout=30,
        json={
            "message": message,
            "tree": new_tree_sha,
            "parents": [base_commit_sha],
        },
    )
    new_commit.raise_for_status()
    new_commit_sha = new_commit.json()["sha"]

    # 4) Branch-Ref auf den neuen Commit setzen (ohne force).
    #    Hat sich die Branch-Spitze zwischenzeitlich bewegt, antwortet
    #    GitHub mit 422 -> wird von gh_update_file_retrying als Konflikt
    #    behandelt und der Vorgang wiederholt.
    upd = requests.patch(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/refs/heads/{branch}",
        headers=gh_headers(), timeout=30,
        json={"sha": new_commit_sha, "force": False},
    )
    upd.raise_for_status()
    return upd.json()

def gh_delete_file(repo, path, message):
    """
    Loescht eine Datei ueber die Git Data API.

    Analog zu gh_put_file, nur dass der Tree-Eintrag fuer den Pfad mit
    sha=None gesetzt wird -- das entfernt die Datei im neuen Tree. Funktioniert
    auch fuer >1-MB-Dateien (die Contents-DELETE-API kann das nicht).

    Gibt True zurueck, wenn geloescht wurde, False wenn die Datei gar nicht
    existierte (idempotent).
    """
    # Existiert die Datei ueberhaupt? Wenn nicht: nichts zu tun.
    _, existing_sha = gh_get_file(repo, path)
    if existing_sha is None:
        return False

    branch = _gh_default_branch(repo)

    ref_url = f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/ref/heads/{branch}"
    ref = requests.get(ref_url, headers=gh_headers(), timeout=30)
    ref.raise_for_status()
    base_commit_sha = ref.json()["object"]["sha"]

    commit = requests.get(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/commits/{base_commit_sha}",
        headers=gh_headers(), timeout=30,
    )
    commit.raise_for_status()
    base_tree_sha = commit.json()["tree"]["sha"]

    # Tree-Eintrag mit sha=None -> GitHub entfernt die Datei.
    tree = requests.post(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/trees",
        headers=gh_headers(), timeout=30,
        json={
            "base_tree": base_tree_sha,
            "tree": [{
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": None,
            }],
        },
    )
    tree.raise_for_status()
    new_tree_sha = tree.json()["sha"]

    new_commit = requests.post(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/commits",
        headers=gh_headers(), timeout=30,
        json={
            "message": message,
            "tree": new_tree_sha,
            "parents": [base_commit_sha],
        },
    )
    new_commit.raise_for_status()
    new_commit_sha = new_commit.json()["sha"]

    upd = requests.patch(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/refs/heads/{branch}",
        headers=gh_headers(), timeout=30,
        json={"sha": new_commit_sha, "force": False},
    )
    upd.raise_for_status()
    return True

def gh_update_file_retrying(repo, path, updater_fn, message):
    """
    Liest die Datei, wendet updater_fn an und schreibt zurueck.

    Bei einem Schreibkonflikt (parallele Aenderung an der Branch-Spitze)
    wird der komplette Read-Modify-Write-Zyklus wiederholt. Da das Schreiben
    jetzt ueber die Git Data API laeuft, meldet sich ein Konflikt beim
    Ref-Update als HTTP 422 ("Update is not a fast forward").

    Gibt updater_fn None zurueck, ist nichts zu schreiben (z.B. ein
    Punkte-Schwung, der nur aus Dubletten bestand) -> der GitHub-Commit
    wird uebersprungen und None zurueckgegeben.
    """
    for attempt in range(3):
        existing, sha = gh_get_file(repo, path)
        new_content = updater_fn(existing)
        if new_content is None:
            return None
        try:
            gh_put_file(repo, path, new_content, message, sha=sha)
            return new_content
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (409, 422) and attempt < 2:
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

def admin_index_remove(track_id):
    """Entfernt einen Track-Eintrag aus dem Admin-Index. Idempotent:
    fehlt der Eintrag (oder die Index-Datei) bereits, passiert nichts."""
    def update(existing):
        if not existing or "tracks" not in existing:
            return None  # nichts zu schreiben
        before = len(existing["tracks"])
        existing["tracks"] = [t for t in existing["tracks"] if t.get("id") != track_id]
        if len(existing["tracks"]) == before:
            return None  # war nicht drin -> kein leerer Commit
        return existing
    gh_update_file_retrying(
        ADMIN_REPO, "index.json", update, f"admin: remove {track_id}"
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

    # Wird vom updater befuellt, damit die Response die echten Zahlen kennt.
    # Liste, damit die innere Closure schreiben kann (kein nonlocal noetig).
    result = {"accepted": 0, "duplicates": 0}

    def update(existing):
        if existing is None:
            raise LookupError("track not found")
        if existing["properties"].get("skipper") != skipper_name:
            raise PermissionError("not owner")
        feat = existing["features"][0]
        coords = feat["geometry"]["coordinates"]
        timestamps = feat["properties"]["timestamps"]

        # Set der bereits vorhandenen Timestamps fuer O(1)-Dublettenpruefung.
        # Macht den Append idempotent: Sendet die App nach einem
        # unklaren Fehlschlag denselben Schwung erneut, werden bereits
        # gespeicherte Punkte uebersprungen statt doppelt eingefuegt.
        seen = set(timestamps)
        accepted = 0
        duplicates = 0

        for p in points:
            t, lat, lon = p["t"], p["lat"], p["lon"]
            if t in seen:
                duplicates += 1
                continue
            seen.add(t)
            # Üblicher Fall: chronologisch hinten anhängen
            if not timestamps or t >= timestamps[-1]:
                timestamps.append(t)
                coords.append([lon, lat])
            else:
                # Out-of-order: an die korrekte Stelle einsortieren
                idx = bisect.bisect_left(timestamps, t)
                timestamps.insert(idx, t)
                coords.insert(idx, [lon, lat])
            accepted += 1

        result["accepted"] = accepted
        result["duplicates"] = duplicates

        # Hat der Schwung ausschliesslich Dubletten enthalten, ist nichts
        # zu schreiben -> None signalisiert gh_update_file_retrying, den
        # GitHub-Commit zu ueberspringen (spart einen leeren Commit).
        if accepted == 0:
            return None

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

    return jsonify({
        "accepted": result["accepted"],
        "duplicates": result["duplicates"],
    })

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

@app.route("/tracks/<track_id>/digest", methods=["GET"])
def get_track_digest(track_id):
    """
    Schlanker Fingerabdruck eines Tracks: nur point_count, last_t und ts_hash
    -- ein paar Bytes statt des kompletten GeoJSON. Die App vergleicht das vor
    dem "Daten vom Geraet entfernen", um sicherzugehen, dass der Server exakt
    die lokale Punktmenge hat. Stimmen Anzahl UND Hash, ist Loeschen sicher.
    """
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
    timestamps = existing["features"][0]["properties"].get("timestamps", [])
    return jsonify({
        "point_count": len(timestamps),
        "last_t": timestamps[-1] if timestamps else None,
        "ts_hash": ts_hash(timestamps),
    })

@app.route("/tracks/<track_id>", methods=["DELETE"])
def delete_track(track_id):
    """
    Loescht einen Track endgueltig: GeoJSON aus dem Daten-Repo + Eintrag aus
    dem Admin-Index. Danach ist der Toern auch ueber den oeffentlichen Link
    (track.html) nicht mehr erreichbar.

    Idempotent: Ist die Datei schon weg (404), gilt das als Erfolg, damit ein
    aus der App-Warteschlange erneut gesendeter Loeschbefehl sauber durchlaeuft
    statt haengenzubleiben. Der Index wird in dem Fall trotzdem bereinigt.
    """
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    file_hash = hash_code(track_id)

    existing, _ = gh_get_file(DATA_REPO, f"tracks/{file_hash}.geojson")
    if existing is None:
        # Schon geloescht -> Index sicherheitshalber aufraeumen, Erfolg melden.
        admin_index_remove(track_id)
        return jsonify({"deleted": True, "already_gone": True})
    if existing["properties"].get("skipper") != skipper_name:
        return jsonify({"error": "not owner"}), 403

    gh_delete_file(DATA_REPO, f"tracks/{file_hash}.geojson",
                   f"delete track {file_hash[:8]}")
    admin_index_remove(track_id)
    return jsonify({"deleted": True})

@app.route("/admin/index", methods=["GET"])
def admin_index():
    if not auth_admin():
        return jsonify({"error": "unauthorized"}), 401
    existing, _ = gh_get_file(ADMIN_REPO, "index.json")
    return jsonify(existing or {"tracks": []})

# ============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
