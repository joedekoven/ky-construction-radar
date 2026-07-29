# Kentucky Construction Radar

A statewide construction intelligence dashboard for Kentucky, modeled on the
Greater Cincinnati Construction Radar. It pulls recent building permits from
Kentucky open data sources, classifies each project by market, scores it for
electrical material opportunity (1-10), and displays everything on a searchable
map + table hosted for free on GitHub Pages.

## How it works

```
scraper.py  -->  projects.json  -->  dashboard.html + app.js
(daily via GitHub Actions)           (static site on GitHub Pages)
```

## Setup (one time, ~10 minutes)

1. Create a new GitHub repository (e.g. `ky-construction-radar`) and upload
   every file in this folder, keeping the folder structure
   (`.github/workflows/update-data.yml` must stay in that path).
2. In the repo: **Settings → Pages → Source: Deploy from a branch →**
   branch `main`, folder `/ (root)`. Save.
3. In the repo: **Actions** tab → enable workflows → open
   "Update construction data" → **Run workflow**. This runs the scraper and
   commits a fresh `projects.json` (replacing the sample data).
4. Your dashboard is live at
   `https://<your-username>.github.io/ky-construction-radar/dashboard.html`
   and refreshes automatically every morning at 6:00 AM Eastern.

To run locally instead: `python3 scraper.py` (no dependencies beyond the
Python standard library), then open `dashboard.html` via any local web server
(`python3 -m http.server`).

## Data sources

### Live today: Louisville Metro (Jefferson County)

Louisville Metro publishes active construction permits through the LOJIC
ArcGIS open data portal, including permit type, work type, project cost,
status, address, coordinates, and the contractor of record. The scraper pages
through the feature layer, keeps commercial-scale work (>= $25,000, excluding
single-family/decks/pools/etc.), and scores each project.

### Add anytime: CSV drop-in (Lexington, statewide, other counties)

There is no single statewide permit API for Kentucky, so the scraper accepts
CSV files placed in the `sources/` folder. Any CSV matching the column format
in `sources/TEMPLATE.csv.example` is merged into the dashboard on the next
run (rows missing coordinates are geocoded automatically via the free US
Census geocoder).

Good candidates:

- **Lexington-Fayette (LFUCG)**: permits run through Accela Citizen Access.
  Export search results to CSV, map the columns to the template, and drop the
  file in `sources/`. Since you're local, Building Inspection can also fulfill
  a recurring report request.
- **Kentucky HBC (statewide commercial)**: the state Department of Housing,
  Buildings and Construction performs plan review for commercial buildings
  statewide, searchable through its eServices portal. No public API, but
  exported results can be dropped in as CSV — this is the single best lever
  for true statewide coverage.
- **Northern Kentucky (Boone/Kenton/Campbell)**: PDS of Kenton County and
  LINK-GIS publish GIS data; check for a permits feature layer, or use CSV.

### Adding another ArcGIS county feed

If a Kentucky city/county publishes a permits FeatureServer (many ArcGIS Hub
"open data" sites do), you can wire it in directly: copy the pattern in
`build_louisville_projects()` in `scraper.py`, point it at the new layer URL,
and adjust the field names. To see a layer's fields, open the layer URL in a
browser — ArcGIS shows the full schema.

## Tuning

All knobs are at the top of `scraper.py`:

- `DAYS_BACK` — lookback window for issue dates (default 90)
- `MIN_VALUE` — minimum project value (default $25,000)
- `EXCLUDE_KEYWORDS` — project types to skip
- Scoring weights live in `electrical_score()` — currently tuned for
  electrical distribution (hospitals, manufacturing, data centers, and
  distilleries score highest)

## Notes

- Permit data reflects what each jurisdiction publishes; coverage and field
  quality vary by county.
- The contractor listed on a permit is often the trade contractor that pulled
  it (electrical/mechanical), which for supply-house purposes is frequently
  exactly who you want to call.
