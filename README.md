# sailtrack-backend

Flask backend for SailTrack. Receives GPS points from the app, commits GeoJSON
to the data repo, maintains the admin index.

## Deploy on Render

1. Push this repo to GitHub (`wolkeyachting/sailtrack-backend`)
2. On Render: New → Blueprint → connect the repo
3. In the Render service settings → Environment, set:
   - `GITHUB_TOKEN` = the fine-grained PAT with write access to `sailtrack` and `sailtrack-admin`
   - `SAILTRACK_TOKENS` = `yourToken123=David` (add more comma-separated: `t1=David,t2=Julian`)
   - `SAILTRACK_ADMIN_TOKEN` = a separate random string only you know

## Endpoints (Phase 1)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/tracks` | Skipper | Create track |
| POST | `/tracks/{id}/points` | Skipper | Append points |
| POST | `/tracks/{id}/finish` | Skipper | Mark finished |
| GET | `/tracks` | Skipper | List own tracks (no codes) |
| GET | `/admin/index` | Admin | All tracks incl. codes |

## Local dev (Windows)

```powershell
python -m pip install -r requirements.txt
$env:GITHUB_TOKEN = "github_pat_..."
$env:SAILTRACK_TOKENS = "dev-token=David"
$env:SAILTRACK_ADMIN_TOKEN = "dev-admin"
python app.py
```
