"""
SailTrack Backend – v3 Datenmodell (Segment-Split)

WARUM v3
--------
Bis v2 lag ein Toern in EINER Datei: ein flacher LineString plus ein
paralleles `timestamps`-Array, sortiert nach t. Das war der Grund, warum der
Uhrsprung vom 19.06.2026 (~+3557 s) so verheerend war: Die falsch gestempelten
Punkte wurden zwischen die zeitgleich aufgenommenen echten Punkte einsortiert.
Aus 220 nm wurden 4341 nm.

Die Zeitbasis der App (clock.dart) verhindert das jetzt. Was bleibt, ist der
Fall, den sie NICHT abfangen kann: Reboot mitten im Toern, Systemuhr steht in
dem Moment falsch -> der neue Anker ist falsch -> die ganze folgende Aufnahme
ist verschoben. Lokal nicht erkennbar (in sich konsistent), und im
Ein-Datei-Modell wuerde sie sich beim Sortieren mit der vorherigen Aufnahme
vermischen.

v3 zieht die Konsequenz:

    Neuer Anker  ->  neues Segment  ->  eigene Datei.

Innerhalb einer Datei gilt Monotonie. UEBER Dateien hinweg wird nie sortiert --
die Reihenfolge ergibt sich aus der Segmentnummer. Zeitliche Ueberlappung
zweier Segmente ist damit ein ANZEIGE-Hinweis (`conflict`), kein Datenschaden.
Und eine nachtraegliche Zeitkorrektur ist ein simples Verschieben genau einer
Datei.

DATEILAYOUT
-----------
    tracks/<hash>/<hash>.geojson        Manifest: Toern-Meta + Segment-Index
    tracks/<hash>/<hash>-s001.geojson   Segment: LineString + timestamps + Anker
    tracks/<hash>/<hash>-s002.geojson

`pauses` faellt ersatzlos weg: Die Luecke zwischen zwei Segmenten IST die Pause.
(Passte schon vorher exakt -- Pausen entstanden genau beim stopTracking.)

Das Manifest traegt pro Segment `point_count` und `ts_hash`. Damit ist
GET /digest ein einziger kleiner Dateizugriff statt eines Vollscans.
"""

import base64
import bisect
import hashlib
import json
import math
import os
import re
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

SEG_RE = re.compile(r"^s\d{3}$")

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

def _norm_ts(timestamps):
    """Kanonische Punkt-Identitaet INNERHALB eines Segments: auf ganze Sekunden
    trunkieren, deduplizieren, sortieren.

    Wichtig: Die Identitaet ist ab v3 (track, segment, t) -- nicht mehr
    (track, t). Zwei Segmente duerfen dieselbe Sekunde tragen; das ist genau
    der Reboot-mit-falscher-Uhr-Fall. Deshalb wird hier IMMER nur ueber die
    Timestamps EINES Segments gerechnet, nie ueber den ganzen Toern.
    """
    return sorted({int(t) for t in timestamps})

def ts_hash(timestamps):
    """
    Fingerabdruck ueber die kanonische Timestamp-Folge EINES SEGMENTS.

    Muss bitgenau zur Dart-Seite passen (fnv1a64Hex in store.dart), die ueber
    die Int-Sekunden der pending_points eines Segments rechnet. FNV-1a, 64 Bit,
    ueber die mit "," verbundenen Dezimal-Strings der sortierten Ints.
    """
    ints = _norm_ts(timestamps)
    s = ",".join(str(x) for x in ints)
    h = 0xcbf29ce484222325
    for b in s.encode("utf-8"):
        h = ((h ^ b) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return format(h, "016x")

# ============================================================================
# Pfade
# ============================================================================

def track_dir(file_hash):
    return f"tracks/{file_hash}"

def manifest_path(file_hash):
    return f"tracks/{file_hash}/{file_hash}.geojson"

def segment_path(file_hash, seg):
    return f"tracks/{file_hash}/{file_hash}-{seg}.geojson"

def legacy_path(file_hash):
    """v2: eine Datei direkt unter tracks/."""
    return f"tracks/{file_hash}.geojson"

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
    Ab 1 MB ist `content` leer und `encoding` == "none" -> der Inhalt muss ueber
    die Git Blob API (per `git_url`) nachgeladen werden. Die Blob-API hat kein
    1-MB-Limit (bis 100 MB).

    Rueckgabe: (parsed_dict_oder_None, blob_sha_oder_None)
    """
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()

    if isinstance(data, list):
        # Pfad ist ein Verzeichnis -- als Datei gelesen ist das ein Fehler.
        return None, None

    if data.get("encoding") == "base64" and data.get("content"):
        raw = base64.b64decode(data["content"])
    else:
        blob = requests.get(data["git_url"], headers=gh_headers(), timeout=30)
        blob.raise_for_status()
        raw = base64.b64decode(blob.json()["content"])

    text = raw.decode("utf-8")
    if not text.strip():
        return None, data["sha"]
    return json.loads(text), data["sha"]

def gh_list_dir(repo, path):
    """Dateinamen eines Verzeichnisses. Leere Liste, wenn es nicht existiert."""
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return [e["path"] for e in data if e.get("type") == "file"]

def _gh_default_branch(repo):
    r = requests.get(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}",
        headers=gh_headers(), timeout=30,
    )
    r.raise_for_status()
    return r.json()["default_branch"]

def gh_commit_files(repo, writes, deletes, message):
    """
    Schreibt und loescht MEHRERE Dateien in EINEM Commit (Git Data API).

    Das ist ab v3 der Normalfall: Ein Punkte-Append fasst Segmentdatei UND
    Manifest an. Beides in einem Commit heisst: Es kann keinen Zustand geben,
    in dem das Manifest andere Zahlen behauptet, als in den Segmenten stehen.
    Zwei getrennte Commits koennten genau dort auseinanderlaufen.

    writes:  {pfad: dict}
    deletes: [pfad, ...]   (nicht existierende Pfade bitte vorher filtern)

    Die Contents-API kann das alles nicht (nur eine Datei, und nur < 1 MB) --
    daher der Blob/Tree/Commit/Ref-Weg, der bis 100 MB traegt.
    """
    if not writes and not deletes:
        return None

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

    tree_entries = []

    for path, content in writes.items():
        payload = json.dumps(content, indent=2, ensure_ascii=False)
        blob = requests.post(
            f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/blobs",
            headers=gh_headers(), timeout=60,
            json={
                "content": base64.b64encode(payload.encode("utf-8")).decode("ascii"),
                "encoding": "base64",
            },
        )
        blob.raise_for_status()
        tree_entries.append({
            "path": path, "mode": "100644", "type": "blob",
            "sha": blob.json()["sha"],
        })

    for path in deletes:
        tree_entries.append({
            "path": path, "mode": "100644", "type": "blob", "sha": None,
        })

    tree = requests.post(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/trees",
        headers=gh_headers(), timeout=60,
        json={"base_tree": base_tree_sha, "tree": tree_entries},
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

    # Ohne force: Hat sich die Branch-Spitze bewegt -> 422 -> Retry im Wrapper.
    upd = requests.patch(
        f"{GH_API}/repos/{GITHUB_OWNER}/{repo}/git/refs/heads/{branch}",
        headers=gh_headers(), timeout=30,
        json={"sha": new_commit_sha, "force": False},
    )
    upd.raise_for_status()
    return upd.json()

def gh_update_files_retrying(repo, read_paths, updater_fn, message):
    """
    Read-Modify-Write ueber mehrere Dateien, mit Retry bei Schreibkonflikt.

    updater_fn(existing: {pfad: dict|None}) -> (writes, deletes) | None
    None bedeutet "nichts zu tun" -> kein leerer Commit.
    """
    for attempt in range(3):
        existing = {}
        for p in read_paths:
            content, _ = gh_get_file(repo, p)
            existing[p] = content

        result = updater_fn(existing)
        if result is None:
            return None
        writes, deletes = result
        if not writes and not deletes:
            return None

        try:
            gh_commit_files(repo, writes, deletes, message)
            return writes
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (409, 422) and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise

def gh_put_file(repo, path, content_dict, message):
    """Einzeldatei -- duenner Wrapper um gh_commit_files."""
    return gh_commit_files(repo, {path: content_dict}, [], message)

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
        data = existing["skippers.json"] or {"skippers": []}
        for s in data.get("skippers", []):
            if s.get("token") == token:
                s["last_seen_at"] = now_iso()
                break
        return {"skippers.json": data}, []

    try:
        gh_update_files_retrying(
            ADMIN_REPO, ["skippers.json"], update, f"last_seen: {token[:8]}..."
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
        data = existing["skippers.json"] or {"skippers": []}
        if any(s.get("token") == token for s in data["skippers"]):
            raise ValueError("collision")
        data["skippers"].append({
            "token": token,
            "name": f"Skipper-{token[:6]}",
            "created_at": now_iso(),
            "last_seen_at": now_iso(),
        })
        return {"skippers.json": data}, []

    gh_update_files_retrying(
        ADMIN_REPO, ["skippers.json"], update, f"skipper new: {token[:8]}..."
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
# Admin-Index
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
        data = existing["skippers.json"] or {"skippers": []}
        for s in data.get("skippers", []):
            if s.get("token") == token:
                s["name"] = new_name
                return {"skippers.json": data}, []
        raise ValueError("skipper not found")

    try:
        gh_update_files_retrying(
            ADMIN_REPO, ["skippers.json"], update, f"admin: rename {token[:8]}"
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"token": token, "name": new_name})

def admin_index_upsert(entry):
    def update(existing):
        data = existing["index.json"] or {"tracks": []}
        for i, t in enumerate(data["tracks"]):
            if t["id"] == entry["id"]:
                data["tracks"][i] = {**t, **entry}
                break
        else:
            data["tracks"].append(entry)
        return {"index.json": data}, []

    gh_update_files_retrying(
        ADMIN_REPO, ["index.json"], update, f"admin: upsert {entry['id']}"
    )

def admin_index_remove(track_id):
    def update(existing):
        data = existing["index.json"]
        if not data or "tracks" not in data:
            return None
        before = len(data["tracks"])
        data["tracks"] = [t for t in data["tracks"] if t.get("id") != track_id]
        if len(data["tracks"]) == before:
            return None
        return {"index.json": data}, []

    gh_update_files_retrying(
        ADMIN_REPO, ["index.json"], update, f"admin: remove {track_id}"
    )

# ============================================================================
# Track-Struktur (v3)
# ============================================================================

def empty_manifest(track_id, name, boat, skipper, trip_start=None, trip_end=None):
    """
    Das Manifest ist die einzige Datei, die das Frontend ueber den Code direkt
    findet. Es traegt die Toern-Meta und den Segment-Index -- aber KEINE Punkte.

    Jeder Segment-Eintrag haelt `point_count` und `ts_hash` redundant zur
    Segmentdatei. Das ist Absicht: GET /digest wird damit zu EINEM kleinen
    Dateizugriff statt zu einem Vollscan ueber alle Segmente.
    """
    return {
        "version": 3,
        "type": "TrackManifest",
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
                "point_count": 0,
                "segment_count": 0,
            },
        },
        "segments": [],
    }

def empty_segment(track_id, seg, anchor=None):
    """
    Eine Segmentdatei ist ein eigenstaendiges GeoJSON -- bewusst so, damit das
    Frontend sie ohne Umbau rendern und ein Nutzer sie einzeln herunterladen
    kann.

    `anchor` dokumentiert, WORAUF sich die Zeitstempel dieses Segments
    beziehen: welcher Wall-Clock-Wert beim Verankern galt, auf welchem Boot,
    und ob dabei ein Uhrsprung erkannt wurde. Genau diese Information hat beim
    Ibiza-Toern gefehlt -- die Rekonstruktion des +3557-s-Versatzes musste
    muehsam aus 67.000 Punkten erfolgen.
    """
    return {
        "type": "FeatureCollection",
        "properties": {
            "version": 3,
            "track_id": track_id,
            "segment": seg,
            "opened_at": now_iso(),
            "closed_at": None,
            "anchor": anchor or {},
            "stats": {
                "distance_nm": 0.0,
                "point_count": 0,
                "t_start": None,
                "t_stop": None,
            },
        },
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": []},
            "properties": {"timestamps": []},
        }],
    }

def recalc_segment(seg_doc):
    """Distanz, Punktzahl, t-Bereich und Hash EINES Segments neu rechnen.

    Kein Pausen-Sonderfall mehr: Ein Segment ist per Definition eine
    ununterbrochene Aufnahme. Wo frueher eine Pause lag, endet heute die Datei.
    """
    feat = seg_doc["features"][0]
    coords = feat["geometry"]["coordinates"]
    timestamps = feat["properties"]["timestamps"]

    total_nm = 0.0
    for j in range(1, len(coords)):
        lon1, lat1 = coords[j-1]
        lon2, lat2 = coords[j]
        total_nm += haversine_nm(lat1, lon1, lat2, lon2)

    stats = {
        "distance_nm": round(total_nm, 3),
        "point_count": len(coords),
        "t_start": timestamps[0] if timestamps else None,
        "t_stop": timestamps[-1] if timestamps else None,
    }
    seg_doc["properties"]["stats"] = stats
    return stats

def segment_entry(seg_doc):
    """Der Eintrag, wie er im Manifest steht."""
    p = seg_doc["properties"]
    st = p["stats"]
    ts = seg_doc["features"][0]["properties"]["timestamps"]
    return {
        "id": p["segment"],
        "file": f"{{hash}}-{p['segment']}.geojson",  # wird unten ersetzt
        "t_start": st["t_start"],
        "t_stop": st["t_stop"],
        "point_count": st["point_count"],
        "distance_nm": st["distance_nm"],
        "ts_hash": ts_hash(ts),
        "anchor": p.get("anchor", {}),
        "opened_at": p.get("opened_at"),
        "closed_at": p.get("closed_at"),
        "closed": p.get("closed_at") is not None,
    }

def manifest_put_segment(manifest, file_hash, seg_doc):
    """Segment-Eintrag im Manifest ersetzen/anlegen und Aggregate neu rechnen."""
    entry = segment_entry(seg_doc)
    entry["file"] = f"{file_hash}-{entry['id']}.geojson"

    segs = manifest.setdefault("segments", [])
    for i, s in enumerate(segs):
        if s["id"] == entry["id"]:
            segs[i] = entry
            break
    else:
        segs.append(entry)
    segs.sort(key=lambda s: s["id"])

    refresh_manifest_stats(manifest)
    return manifest

def refresh_manifest_stats(manifest):
    """
    Aggregate + Konfliktmarkierung.

    `conflict` heisst: Der t-Bereich dieses Segments ueberlappt den eines
    anderen. Das ist KEIN Datenschaden (die Dateien sind getrennt, es wird nie
    ueber Segmentgrenzen sortiert), sondern ein Hinweis, dass mindestens einer
    der beiden Anker daneben lag. Die App zeigt das an; korrigiert wird per
    /shift, in Ruhe, im Nachhinein.
    """
    segs = manifest.get("segments", [])

    total_nm = sum(s.get("distance_nm") or 0.0 for s in segs)
    total_pts = sum(s.get("point_count") or 0 for s in segs)

    for s in segs:
        s["conflict"] = False
    for i, a in enumerate(segs):
        if a["t_start"] is None or a["t_stop"] is None:
            continue
        for b in segs[i+1:]:
            if b["t_start"] is None or b["t_stop"] is None:
                continue
            if a["t_start"] <= b["t_stop"] and b["t_start"] <= a["t_stop"]:
                a["conflict"] = True
                b["conflict"] = True

    manifest["properties"]["stats"] = {
        "distance_total_nm": round(total_nm, 3),
        "point_count": total_pts,
        "segment_count": len(segs),
    }
    return manifest

def check_owner(manifest, skipper_name):
    if manifest is None:
        raise LookupError("track not found")
    if manifest["properties"].get("skipper") != skipper_name:
        raise PermissionError("not owner")

# ============================================================================
# Track-Endpoints
# ============================================================================

@app.route("/")
def root():
    return jsonify({
        "service": "sailtrack-backend",
        "status": "ok",
        "version": 3,
        "model": "segment-split",
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
    manifest = empty_manifest(code, name, boat, skipper_name, trip_start, trip_end)

    gh_put_file(DATA_REPO, manifest_path(file_hash), manifest,
                f"create track {file_hash[:8]}")
    admin_index_upsert({
        "id": code, "code": code, "file_hash": file_hash,
        "name": name, "boat": boat, "skipper": skipper_name,
        "trip_start": trip_start, "trip_end": trip_end,
        "status": "active", "created_at": manifest["properties"]["created_at"],
    })
    return jsonify({"id": code, "code": code, "file_hash": file_hash})

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
    mpath = manifest_path(file_hash)

    def update(existing):
        m = existing[mpath]
        check_owner(m, skipper_name)
        for k, v in updates.items():
            m["properties"][k] = v
        return {mpath: m}, []

    try:
        gh_update_files_retrying(
            DATA_REPO, [mpath], update,
            f"patch {file_hash[:8]}: {','.join(updates.keys())}"
        )
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    admin_index_upsert({"id": track_id, **updates})
    return jsonify({"id": track_id, **updates})

# ============================================================================
# Segmente
# ============================================================================

@app.route("/tracks/<track_id>/segments", methods=["POST"])
def open_segment(track_id):
    """
    Oeffnet ein Segment. Body: {seg: "s001", anchor: {...}}

    Idempotent: Existiert das Segment schon, ist das ein Erfolg -- die App darf
    den Aufruf nach einem unklaren Fehlschlag gefahrlos wiederholen. Der Anker
    wird dabei NICHT ueberschrieben; er gehoert dem Segment und aendert sich nur
    ueber /shift.
    """
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper

    body = request.get_json(force=True, silent=True) or {}
    seg = (body.get("seg") or "").strip()
    if not SEG_RE.match(seg):
        return jsonify({"error": "seg must look like s001"}), 400
    anchor = body.get("anchor") or {}

    file_hash = hash_code(track_id)
    mpath = manifest_path(file_hash)
    spath = segment_path(file_hash, seg)

    def update(existing):
        m = existing[mpath]
        check_owner(m, skipper_name)
        s = existing[spath]
        if s is not None:
            return None  # schon da -> nichts zu tun
        s = empty_segment(track_id, seg, anchor)
        recalc_segment(s)
        manifest_put_segment(m, file_hash, s)
        return {spath: s, mpath: m}, []

    try:
        gh_update_files_retrying(
            DATA_REPO, [mpath, spath], update,
            f"open segment {seg} on {file_hash[:8]}"
        )
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    return jsonify({"seg": seg, "file": f"{file_hash}-{seg}.geojson"})

@app.route("/tracks/<track_id>/segments/<seg>/points", methods=["POST"])
def append_points(track_id, seg):
    """
    Body: {points: [{t, lat, lon}, ...]}

    Dubletten werden uebersprungen -- INNERHALB dieses Segments. Sendet die App
    nach einem unklaren Fehlschlag denselben Schwung erneut, passiert nichts
    Doppeltes. Dass dieselbe Sekunde in einem ANDEREN Segment vorkommt, ist
    dagegen voellig in Ordnung und wird hier nicht geprueft: Ab v3 ist die
    Punkt-Identitaet (track, segment, t).
    """
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper

    if not SEG_RE.match(seg):
        return jsonify({"error": "bad seg"}), 400

    body = request.get_json(force=True, silent=True) or {}
    points = body.get("points", [])
    if not points:
        return jsonify({"accepted": 0, "duplicates": 0})
    points = sorted(points, key=lambda p: p["t"])

    file_hash = hash_code(track_id)
    mpath = manifest_path(file_hash)
    spath = segment_path(file_hash, seg)

    result = {"accepted": 0, "duplicates": 0}

    def update(existing):
        m = existing[mpath]
        check_owner(m, skipper_name)
        s = existing[spath]
        if s is None:
            raise LookupError("segment not found")

        feat = s["features"][0]
        coords = feat["geometry"]["coordinates"]
        timestamps = feat["properties"]["timestamps"]
        seen = set(timestamps)

        accepted = 0
        duplicates = 0
        for p in points:
            t, lat, lon = int(p["t"]), p["lat"], p["lon"]
            if t in seen:
                duplicates += 1
                continue
            seen.add(t)
            if not timestamps or t >= timestamps[-1]:
                timestamps.append(t)
                coords.append([lon, lat])
            else:
                idx = bisect.bisect_left(timestamps, t)
                timestamps.insert(idx, t)
                coords.insert(idx, [lon, lat])
            accepted += 1

        result["accepted"] = accepted
        result["duplicates"] = duplicates
        if accepted == 0:
            return None

        recalc_segment(s)
        manifest_put_segment(m, file_hash, s)
        return {spath: s, mpath: m}, []

    try:
        gh_update_files_retrying(
            DATA_REPO, [mpath, spath], update,
            f"append {len(points)} pts to {file_hash[:8]}/{seg}"
        )
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    return jsonify(result)

@app.route("/tracks/<track_id>/segments/<seg>/close", methods=["POST"])
def close_segment(track_id, seg):
    """Segment schliessen. Danach kommen keine Punkte mehr dazu.

    Nicht erzwungen -- der Server bleibt dumm. Es ist eine Markierung fuer die
    App und das Frontend, kein Schreibschutz.
    """
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    if not SEG_RE.match(seg):
        return jsonify({"error": "bad seg"}), 400

    file_hash = hash_code(track_id)
    mpath = manifest_path(file_hash)
    spath = segment_path(file_hash, seg)

    def update(existing):
        m = existing[mpath]
        check_owner(m, skipper_name)
        s = existing[spath]
        if s is None:
            raise LookupError("segment not found")
        if s["properties"].get("closed_at"):
            return None
        s["properties"]["closed_at"] = now_iso()
        recalc_segment(s)
        manifest_put_segment(m, file_hash, s)
        return {spath: s, mpath: m}, []

    try:
        gh_update_files_retrying(
            DATA_REPO, [mpath, spath], update,
            f"close segment {seg} on {file_hash[:8]}"
        )
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    return jsonify({"seg": seg, "closed": True})

@app.route("/tracks/<track_id>/segments/<seg>/shift", methods=["POST"])
def shift_segment(track_id, seg):
    """
    Verschiebt ALLE Zeitstempel eines Segments um `delta` Sekunden.
    Body: {delta: -3557}

    Das ist die Reparatur fuer einen falschen Anker (Reboot mitten im Toern,
    Systemuhr stand daneben). Weil alle Stempel um denselben Betrag wandern,
    bleibt die Reihenfolge erhalten -- es muss nichts umsortiert werden, und es
    kann innerhalb des Segments keine Dublette entstehen.

    Eine Ueberlappung mit einem anderen Segment ist ausdruecklich ERLAUBT und
    wird nur als `conflict` markiert. Getrennte Dateien, keine gemeinsame
    Sortierung -- das ist der ganze Sinn des Splits.

    Der Anker der Datei wandert mit, sonst wuerde ein spaeteres Fortsetzen der
    Aufnahme wieder auf die alte Zeitbasis zurueckfallen.
    """
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    if not SEG_RE.match(seg):
        return jsonify({"error": "bad seg"}), 400

    body = request.get_json(force=True, silent=True) or {}
    delta = body.get("delta")
    if not isinstance(delta, int) or delta == 0:
        return jsonify({"error": "delta must be a non-zero int (seconds)"}), 400

    file_hash = hash_code(track_id)
    mpath = manifest_path(file_hash)
    spath = segment_path(file_hash, seg)

    def update(existing):
        m = existing[mpath]
        check_owner(m, skipper_name)
        s = existing[spath]
        if s is None:
            raise LookupError("segment not found")

        feat = s["features"][0]
        feat["properties"]["timestamps"] = [
            int(t) + delta for t in feat["properties"]["timestamps"]
        ]

        anchor = s["properties"].setdefault("anchor", {})
        if isinstance(anchor.get("wall_anchor_ms"), int):
            anchor["wall_anchor_ms"] += delta * 1000
        hist = anchor.setdefault("shifts", [])
        hist.append({"delta": delta, "at": now_iso()})

        recalc_segment(s)
        manifest_put_segment(m, file_hash, s)
        return {spath: s, mpath: m}, []

    try:
        gh_update_files_retrying(
            DATA_REPO, [mpath, spath], update,
            f"shift {seg} by {delta}s on {file_hash[:8]}"
        )
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403

    m, _ = gh_get_file(DATA_REPO, mpath)
    entry = next((x for x in m["segments"] if x["id"] == seg), None)
    return jsonify({"seg": seg, "delta": delta, "segment": entry})

# ============================================================================
# Status
# ============================================================================

def _set_status(track_id, new_status, skipper_name):
    file_hash = hash_code(track_id)
    mpath = manifest_path(file_hash)

    def update(existing):
        m = existing[mpath]
        check_owner(m, skipper_name)
        m["properties"]["status"] = new_status
        if new_status == "finished":
            m["properties"]["finished_at"] = now_iso()
        else:
            m["properties"].pop("finished_at", None)
        return {mpath: m}, []

    written = gh_update_files_retrying(
        DATA_REPO, [mpath], update, f"status->{new_status} {file_hash[:8]}"
    )
    admin_index_upsert({"id": track_id, "status": new_status})
    return written[mpath] if written else None

@app.route("/tracks/<track_id>/finish", methods=["POST"])
def finish_track(track_id):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    try:
        m = _set_status(track_id, "finished", skipper_name)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    return jsonify({"status": "finished",
                    "stats": m["properties"]["stats"] if m else None})

@app.route("/tracks/<track_id>/reopen", methods=["POST"])
def reopen_track(track_id):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    try:
        m = _set_status(track_id, "active", skipper_name)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    return jsonify({"status": "active",
                    "stats": m["properties"]["stats"] if m else None})

# ============================================================================
# Lesen
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
    """Nur das Manifest -- klein, ohne Punkte."""
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    m, _ = gh_get_file(DATA_REPO, manifest_path(hash_code(track_id)))
    try:
        check_owner(m, skipper_name)
    except LookupError:
        return jsonify({"error": "track not found"}), 404
    except PermissionError:
        return jsonify({"error": "not owner"}), 403
    return jsonify(m)

@app.route("/tracks/<track_id>/segments/<seg>", methods=["GET"])
def get_segment(track_id, seg):
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper
    if not SEG_RE.match(seg):
        return jsonify({"error": "bad seg"}), 400

    file_hash = hash_code(track_id)
    m, _ = gh_get_file(DATA_REPO, manifest_path(file_hash))
    try:
        check_owner(m, skipper_name)
    except LookupError:
        return jsonify({"error": "track not found"}), 404
    except PermissionError:
        return jsonify({"error": "not owner"}), 403

    s, _ = gh_get_file(DATA_REPO, segment_path(file_hash, seg))
    if s is None:
        return jsonify({"error": "segment not found"}), 404
    return jsonify(s)

@app.route("/tracks/<track_id>/digest", methods=["GET"])
def get_track_digest(track_id):
    """
    Fingerabdruck PRO SEGMENT -- aus dem Manifest, ohne die Segmentdateien
    anzufassen. Ein Dateizugriff, ein paar hundert Bytes.

    Die App vergleicht das vor "Daten vom Geraet entfernen": Stimmen fuer jedes
    Segment point_count UND ts_hash mit dem lokalen Bestand, ist Loeschen sicher.
    """
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper

    m, _ = gh_get_file(DATA_REPO, manifest_path(hash_code(track_id)))
    try:
        check_owner(m, skipper_name)
    except LookupError:
        return jsonify({"error": "track not found"}), 404
    except PermissionError:
        return jsonify({"error": "not owner"}), 403

    segs = [{
        "id": s["id"],
        "point_count": s["point_count"],
        "last_t": s["t_stop"],
        "ts_hash": s["ts_hash"],
        "closed": s.get("closed", False),
        "conflict": s.get("conflict", False),
    } for s in m.get("segments", [])]

    return jsonify({
        "segments": segs,
        "point_count": m["properties"]["stats"]["point_count"],
        "segment_count": len(segs),
    })

@app.route("/tracks/<track_id>", methods=["DELETE"])
def delete_track(track_id):
    """Manifest + alle Segmentdateien, in einem Commit. Idempotent."""
    skipper = auth_skipper()
    if not skipper:
        return jsonify({"error": "unauthorized"}), 401
    skipper_name, _ = skipper

    file_hash = hash_code(track_id)
    m, _ = gh_get_file(DATA_REPO, manifest_path(file_hash))
    if m is None:
        admin_index_remove(track_id)
        return jsonify({"deleted": True, "already_gone": True})
    if m["properties"].get("skipper") != skipper_name:
        return jsonify({"error": "not owner"}), 403

    paths = gh_list_dir(DATA_REPO, track_dir(file_hash))
    if paths:
        gh_commit_files(DATA_REPO, {}, paths, f"delete track {file_hash[:8]}")
    admin_index_remove(track_id)
    return jsonify({"deleted": True, "files": len(paths)})

# ============================================================================
# Migration v2 -> v3
# ============================================================================

def split_v2_at_pauses(coords, timestamps, pauses):
    """
    Zerlegt den flachen v2-LineString an den Pausen.

    Identisch zur Logik, die das alte Frontend (track.html) benutzt hat --
    absichtlich, damit die Migration genau die Segmente erzeugt, die man dort
    schon gesehen hat. Pausen entstanden beim stopTracking, ein Abschnitt
    zwischen zwei Pausen ist also genau eine Aufnahme.
    """
    pauses_sorted = sorted(pauses or [])
    out = []
    cur_c, cur_t = [], []
    pi = 0
    for i, t in enumerate(timestamps):
        while pi < len(pauses_sorted) and pauses_sorted[pi] < t:
            if cur_c:
                out.append((cur_c, cur_t))
                cur_c, cur_t = [], []
            pi += 1
        cur_c.append(coords[i])
        cur_t.append(int(t))
    if cur_c:
        out.append((cur_c, cur_t))
    return out

def migrate_one(track_id, dry_run=False):
    """Einen v2-Track ins v3-Layout ueberfuehren. Gibt eine Zusammenfassung."""
    file_hash = hash_code(track_id)
    old, _ = gh_get_file(DATA_REPO, legacy_path(file_hash))
    if old is None:
        return {"id": track_id, "skipped": "no v2 file"}

    props = old["properties"]
    feat = old["features"][0]
    coords = feat["geometry"]["coordinates"]
    timestamps = feat["properties"].get("timestamps", [])
    pauses = props.get("pauses", [])

    parts = split_v2_at_pauses(coords, timestamps, pauses)

    manifest = empty_manifest(
        track_id, props.get("name", ""), props.get("boat", "–"),
        props.get("skipper", ""), props.get("trip_start"), props.get("trip_end"),
    )
    manifest["properties"]["status"] = props.get("status", "active")
    manifest["properties"]["created_at"] = props.get("created_at", now_iso())
    if props.get("finished_at"):
        manifest["properties"]["finished_at"] = props["finished_at"]
    manifest["properties"]["migrated_from"] = "v2"

    writes = {}
    for i, (c, t) in enumerate(parts, start=1):
        seg = f"s{i:03d}"
        s = empty_segment(track_id, seg, anchor={
            # Der Anker ist fuer Alt-Toerns unbekannt -- es gab ihn nie.
            # Ehrlich als null markieren statt etwas zu erfinden.
            "known": False,
            "migrated": True,
        })
        s["features"][0]["geometry"]["coordinates"] = c
        s["features"][0]["properties"]["timestamps"] = t
        s["properties"]["opened_at"] = None
        s["properties"]["closed_at"] = now_iso()
        recalc_segment(s)
        manifest_put_segment(manifest, file_hash, s)
        writes[segment_path(file_hash, seg)] = s

    writes[manifest_path(file_hash)] = manifest

    summary = {
        "id": track_id,
        "segments": len(parts),
        "points": sum(len(t) for _, t in parts),
        "distance_nm": manifest["properties"]["stats"]["distance_total_nm"],
        "old_distance_nm": (props.get("stats") or {}).get("distance_total_nm"),
    }
    if dry_run:
        summary["dry_run"] = True
        return summary

    gh_commit_files(
        DATA_REPO, writes, [legacy_path(file_hash)],
        f"migrate {file_hash[:8]} v2->v3 ({len(parts)} segments)"
    )
    return summary

@app.route("/admin/migrate", methods=["POST"])
def admin_migrate():
    """
    Migriert alle im Admin-Index gelisteten Toerns.

    Body: {"dry_run": true}  -> nichts wird geschrieben, nur gerechnet.
    Body: {"ids": ["ABC..."]} -> nur diese.

    Vorher pruefen lohnt: `distance_nm` (neu, ohne Pausen-Sonderfall) und
    `old_distance_nm` (v2, das die Pausen uebersprang) muessen praktisch gleich
    sein. Weichen sie ab, lag im alten Bestand ein Punkt ueber einer Pause --
    dann bitte nicht blind schreiben, sondern nachschauen.
    """
    if not auth_admin():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(force=True, silent=True) or {}
    dry_run = bool(body.get("dry_run"))
    only = body.get("ids")

    idx, _ = gh_get_file(ADMIN_REPO, "index.json")
    tracks = (idx or {}).get("tracks", [])
    ids = [t["id"] for t in tracks]
    if only:
        ids = [i for i in ids if i in only]

    out = []
    for tid in ids:
        try:
            out.append(migrate_one(tid, dry_run=dry_run))
        except Exception as e:
            out.append({"id": tid, "error": str(e)})
    return jsonify({"dry_run": dry_run, "count": len(out), "results": out})

# ============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
