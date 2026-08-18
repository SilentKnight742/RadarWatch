# Streamlit deployment

The hosted app must never acquire data or process rasters. Deployment occurs
only after a successful local `publish` stage creates `demo_data/valencia`.

## Pre-deployment checks

```powershell
uv sync --all-extras --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run radarwatch run --config configs/valencia.yaml --from-stage publish --offline
```

Confirm:

- `demo_data/valencia/manifest.json` reports less than 30 MiB;
- the exact-time overlap is non-zero;
- no absolute path or credential appears in the public JSON;
- all counts retain exposure/intersection terminology; and
- `streamlit run app.py` performs no data acquisition or inference request
  (the contextual basemap still loads attributed public tiles).

## Community Cloud

1. Commit the generated demo bundle intentionally; raw/intermediate data stays
   ignored.
2. Connect the public GitHub repository in Streamlit Community Cloud.
3. Select `app.py` as the entry point and Python 3.12 as the runtime.
4. Do not create Earthdata or API secrets in the hosted app.
5. Add the resulting URL and a screenshot/GIF to the README.

`requirements.txt` contains only lightweight app dependencies. The complete
offline pipeline is locked by `pyproject.toml` and `uv.lock`.
