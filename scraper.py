"""
Kentucky Construction Radar - data scraper

Pulls recent construction permit activity from Kentucky open data
sources, classifies each project by market, scores it for electrical
material opportunity, and writes projects.json for the dashboard.

Sources:
  1. Louisville Metro (Jefferson County) - ArcGIS open data feed
     published by LOJIC / Louisville Metro Construction Review.
  2. Any CSV files dropped into the sources/ folder (for Lexington
     Accela exports, KY HBC statewide exports, or any other county).
     See README.md for the expected column format.

Run:  python3 scraper.py
"""

import csv
import glob
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


# ============================================================
# CONFIGURATION
# ============================================================

# Louisville Metro / LOJIC ArcGIS feature layers.
# The scraper tries each URL in order and uses the first one
# that returns data (service names occasionally change).
LOUISVILLE_LAYER_URLS = [
    (
        "https://services1.arcgis.com/79kfd2K6fskCAkyg/arcgis/rest/"
        "services/Louisville_Metro_KY_Active_Construction_Permits/"
        "FeatureServer/0"
    ),
    (
        "https://services1.arcgis.com/79kfd2K6fskCAkyg/arcgis/rest/"
        "services/Louisville_Metro_KY_Active_Permits/"
        "FeatureServer/0"
    ),
    (
        "https://services1.arcgis.com/79kfd2K6fskCAkyg/arcgis/rest/"
        "services/Louisville_Metro_KY_All_Permits_(Historical)/"
        "FeatureServer/0"
    ),
]

LOUISVILLE_SOURCE_LABEL = "Louisville Metro Construction Review"

LOUISVILLE_PORTAL_URL = (
    "https://louisvilleky.gov/government/construction-review/"
    "services/online-permit-search"
)

# ------------------------------------------------------------
# YOUR BRANCH LOCATION - proximity scoring is measured from here.
# Set this to your store's address coordinates (right-click the
# spot in Google Maps and copy the numbers).
# ------------------------------------------------------------
BRANCH_NAME = "Lexington branch"
BRANCH_LATITUDE = 38.0406
BRANCH_LONGITUDE = -84.5037

# How far back we look for permits (by issue date)
DAYS_BACK = 90

# If the date filter wipes out everything (some county feeds are
# point-in-time snapshots with old issue dates), keep this many of
# the highest-value permits anyway so the dashboard is never empty.
FALLBACK_KEEP = 150

# Ignore most tiny projects
MIN_VALUE = 25_000

GEOCODE_CACHE_FILE = "geocode_cache.json"

OUTPUT_FILE = "projects.json"

CSV_SOURCE_FOLDER = "sources"


# Residential IS in scope. Only skip trivial work types that will
# never move meaningful material.
EXCLUDE_KEYWORDS = [
    "deck",
    "fence",
    "swimming pool",
    "above ground pool",
    "inground pool",
    "shed",
    "carport",
    "demolition",
    "wrecking",
    "sign permit",
]


# ============================================================
# BASIC HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def parse_money(value):
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    text = (
        str(value)
        .replace("$", "")
        .replace(",", "")
        .strip()
    )
    try:
        return float(text or 0)
    except (TypeError, ValueError):
        return 0


def format_money(number):
    if not number:
        return "Unknown"
    return f"${number:,.0f}"


def parse_date(value):
    """
    Handle the date formats that show up in county feeds:
    ISO strings, US-style strings, and Esri epoch milliseconds.
    Returns a timezone-aware datetime or None.
    """
    if value is None or value == "":
        return None

    # Esri date fields arrive as epoch milliseconds
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(
                float(value) / 1000.0,
                tz=timezone.utc,
            )
        except (ValueError, OSError, OverflowError):
            return None

    text = clean_text(value)

    formats = [
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
        "%m/%d/%y",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(text[:26], fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def http_get_json(url, timeout=60):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "KYConstructionRadar/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# ============================================================
# GEOCODING (US Census, free, cached)
# ============================================================

def load_geocode_cache():
    try:
        with open(GEOCODE_CACHE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_geocode_cache(cache):
    with open(GEOCODE_CACHE_FILE, "w", encoding="utf-8") as file:
        json.dump(cache, file, indent=2, ensure_ascii=False)


def geocode_address(address, city, state, cache):
    if not address or address == "Unknown":
        return None, None

    cache_key = f"{address}|{city}|{state}".upper().strip()

    if cache_key in cache:
        cached = cache[cache_key]
        return cached.get("latitude"), cached.get("longitude")

    query = {
        "street": address,
        "city": city,
        "state": state or "KY",
        "benchmark": "Public_AR_Current",
        "format": "json",
    }

    url = (
        "https://geocoding.geo.census.gov/"
        "geocoder/locations/address?"
        + urllib.parse.urlencode(query)
    )

    try:
        data = http_get_json(url, timeout=30)
        matches = data.get("result", {}).get("addressMatches", [])

        if matches:
            coordinates = matches[0].get("coordinates", {})
            latitude = coordinates.get("y")
            longitude = coordinates.get("x")
            cache[cache_key] = {
                "latitude": latitude,
                "longitude": longitude,
            }
            return latitude, longitude

    except Exception as error:
        print(
            f"WARNING: Could not geocode "
            f"{address}, {city}, {state}: {error}"
        )

    cache[cache_key] = {"latitude": None, "longitude": None}
    return None, None


# ============================================================
# MARKET CLASSIFICATION
# ============================================================

def classify_market(description, proposed_use, permit_type, workclass):

    text = " ".join([
        clean_text(description),
        clean_text(proposed_use),
        clean_text(permit_type),
        clean_text(workclass),
    ]).lower()

    if any(word in text for word in [
        "manufacturing", "factory", "industrial", "plant",
        "production facility", "processing facility",
        "assembly facility", "f-1", "f-2",
    ]):
        return "Industrial"

    if any(word in text for word in [
        "warehouse", "distribution center", "distribution facility",
        "logistics", "fulfillment", "storage facility", "s-1", "s-2",
    ]):
        return "Warehouse / Logistics"

    if any(word in text for word in [
        "hospital", "medical", "clinic", "healthcare", "health care",
        "surgery center", "urgent care", "nursing",
    ]):
        return "Healthcare"

    if any(word in text for word in [
        "school", "university", "college", "education",
        "classroom", "campus", "daycare", "day care",
    ]):
        return "Education"

    if any(word in text for word in [
        "apartment", "apartments", "multifamily", "multi-family",
        "condominium", "condominiums", "student housing",
        "senior living", "r-1", "r-2", "1-2-3 fm",
    ]):
        return "Multifamily"

    if any(word in text for word in [
        "single family", "single-family", "sngl fam", "duplex",
        "two family", "2 family", "townhome", "townhouse",
        "dwelling", "residence", "residential", "r-3", "r-4",
        "home addition", "basement finish", "garage",
    ]):
        return "Residential"

    if any(word in text for word in [
        "hotel", "motel", "hospitality", "lodging", "resort",
    ]):
        return "Hospitality"

    if any(word in text for word in [
        "government", "municipal", "city hall", "fire station",
        "police station", "courthouse", "public works",
        "library", "community center",
    ]):
        return "Government / Public"

    if any(word in text for word in [
        "utility", "water treatment", "wastewater", "sewer",
        "substation", "infrastructure", "transit", "pump station",
    ]):
        return "Infrastructure / Utility"

    if any(word in text for word in ["mixed use", "mixed-use"]):
        return "Mixed-Use"

    if any(word in text for word in [
        "commercial", "office", "retail", "restaurant", "store",
        "tenant", "shopping", "business", "bank", "grocery",
        "supermarket", "bar", "a-1", "a-2", "a-3", "a-4", "a-5",
        "bourbon", "distillery", "brewery",
    ]):
        return "Commercial"

    return "Other"


# ============================================================
# OPPORTUNITY SCORE  =  DOLLARS (0-5)  +  PROXIMITY (0-5)
# ============================================================

import math


def distance_miles(latitude, longitude):
    """Great-circle distance from the branch, in miles."""
    if latitude is None or longitude is None:
        return None
    try:
        lat1 = math.radians(BRANCH_LATITUDE)
        lon1 = math.radians(BRANCH_LONGITUDE)
        lat2 = math.radians(float(latitude))
        lon2 = math.radians(float(longitude))
    except (TypeError, ValueError):
        return None

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )
    return round(3958.8 * 2 * math.asin(math.sqrt(a)), 1)


def value_points(value):
    if value >= 20_000_000:
        return 5, "very large project value"
    if value >= 5_000_000:
        return 4, "large project value"
    if value >= 1_000_000:
        return 3, "significant project value"
    if value >= 250_000:
        return 2, "solid project value"
    if value >= 100_000:
        return 1, "moderate project value"
    return 0, None


def proximity_points(miles):
    if miles is None:
        return 0, "distance unknown"
    if miles <= 10:
        return 5, f"{miles} mi from {BRANCH_NAME}"
    if miles <= 25:
        return 4, f"{miles} mi from {BRANCH_NAME}"
    if miles <= 50:
        return 3, f"{miles} mi from {BRANCH_NAME}"
    if miles <= 75:
        return 2, f"{miles} mi from {BRANCH_NAME}"
    if miles <= 100:
        return 1, f"{miles} mi from {BRANCH_NAME}"
    return 0, f"{miles} mi from {BRANCH_NAME} (outside radius)"


def opportunity_score(value, miles):
    points_v, reason_v = value_points(value)
    points_p, reason_p = proximity_points(miles)

    score = min(max(points_v + points_p, 1), 10)

    reasons = [reason for reason in [reason_p, reason_v] if reason]
    if not reasons:
        reasons.append("potential material opportunity")

    return score, "; ".join(reasons)


# ============================================================
# SHARED FILTERS
# ============================================================

def is_excluded(text):
    lowered = text.lower()
    return any(word in lowered for word in EXCLUDE_KEYWORDS)


def within_lookback(issued):
    if issued is None:
        # Keep undated records; active permit feeds are current
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    return issued >= cutoff


# ============================================================
# SOURCE 1: LOUISVILLE METRO (ArcGIS)
# ============================================================

def fetch_arcgis_features(layer_url, max_records=20_000):
    """
    Page through an ArcGIS feature layer, newest rows first,
    and return up to max_records rows.
    """

    # Ask the layer for its object ID field name so we can sort
    # descending (newest records have the highest IDs).
    order_field = None
    try:
        info = http_get_json(layer_url + "?f=json")
        order_field = info.get("objectIdField") or info.get(
            "objectIdFieldName"
        )
    except Exception:
        pass

    features = []
    offset = 0
    page_size = 1000

    while True:
        query = {
            "where": "1=1",
            "outFields": "*",
            "f": "json",
            "resultOffset": str(offset),
            "resultRecordCount": str(page_size),
        }

        if order_field:
            query["orderByFields"] = f"{order_field} DESC"

        url = layer_url + "/query?" + urllib.parse.urlencode(query)
        data = http_get_json(url)

        if "error" in data:
            raise RuntimeError(data["error"])

        page = data.get("features", [])
        features.extend(page)

        if len(page) < page_size:
            break

        offset += page_size

        # Safety valve
        if offset >= max_records:
            break

    return features


def get_field(attributes, *names):
    """Case-insensitive attribute lookup across possible field names."""
    lowered = {
        str(key).lower(): value
        for key, value in attributes.items()
    }
    for name in names:
        if name.lower() in lowered:
            value = lowered[name.lower()]
            if value not in (None, ""):
                return value
    return None


def build_louisville_projects():

    def has_recent_data(rows):
        """True if any of the first rows was issued in the window."""
        for row in rows[:300]:
            issued = parse_date(
                get_field(
                    row.get("attributes", {}),
                    "ISSUEDATE", "IssueDate", "ISSUEDDATE",
                )
            )
            if issued is not None and within_lookback(issued):
                return True
        return False

    features = []
    backup_features = []

    for layer_url in LOUISVILLE_LAYER_URLS:
        try:
            print(f"Trying Louisville layer:\n  {layer_url}")
            rows = fetch_arcgis_features(layer_url)

            if not rows:
                print("  ...returned no rows, trying next URL.")
                continue

            if has_recent_data(rows):
                print(f"  ...has recent permits. Using this layer.")
                features = rows
                break

            print(
                "  ...responded but has no permits in the lookback "
                "window (stale snapshot?). Keeping as backup."
            )
            if not backup_features:
                backup_features = rows

        except Exception as error:
            print(f"  WARNING: layer failed ({error}), trying next URL.")

    if not features and backup_features:
        print("No layer had recent data; using best available backup.")
        features = backup_features

    print(f"Downloaded {len(features)} Louisville permit records.")

    projects = []
    skipped = {"excluded type": 0, "below min value": 0}

    for feature in features:
        attributes = feature.get("attributes", {})

        permit_number = clean_text(
            get_field(attributes, "PERMITNUMBER", "PermitNumber")
        )
        permit_type = clean_text(
            get_field(attributes, "PERMITTYPE", "PermitType")
        )
        category = clean_text(
            get_field(attributes, "CATEGORYNAME", "CategoryName")
        )
        work_type = clean_text(
            get_field(attributes, "WORKTYPE", "WorkType")
        )
        status = clean_text(get_field(attributes, "STATUS", "Status"))
        contractor = clean_text(
            get_field(attributes, "CONTRACTOR", "Contractor")
        )
        address = clean_text(get_field(attributes, "ADDRESS", "Address"))
        city = clean_text(
            get_field(attributes, "CITY", "City")
        ) or "Louisville"
        zipcode = clean_text(get_field(attributes, "ZIPCODE", "ZipCode"))
        neighborhood = clean_text(
            get_field(attributes, "NEIGHBORHOOD", "Neighborhood")
        )
        square_feet = get_field(attributes, "SQUAREFEET", "SquareFeet")

        value_numeric = parse_money(
            get_field(attributes, "PROJECTCOSTS", "ProjectCosts")
        )

        issued = parse_date(
            get_field(attributes, "ISSUEDATE", "IssueDate", "ISSUEDDATE")
        )

        latitude = get_field(attributes, "Latitude", "LAT")
        longitude = get_field(attributes, "Longitude", "LON", "LONG")

        combined_text = " ".join(
            [permit_type, category, work_type]
        )

        # Filters (date handled after the loop so we can fall back)
        if is_excluded(combined_text):
            skipped["excluded type"] += 1
            continue
        if value_numeric < MIN_VALUE:
            skipped["below min value"] += 1
            continue

        description = " - ".join(
            part for part in [permit_type, work_type] if part
        ) or "Construction Permit"

        title_use = category or permit_type or "Project"
        project_name = f"{title_use} - {address}" if address else title_use

        projects.append({
            "project": project_name,
            "address": address,
            "city": city,
            "county": "Jefferson",
            "state": "KY",
            "zipcode": zipcode,
            "neighborhood": neighborhood,
            "square_feet": square_feet,
            "latitude": (
                float(latitude)
                if latitude not in (None, "") else None
            ),
            "longitude": (
                float(longitude)
                if longitude not in (None, "") else None
            ),
            "type": permit_type or "Building",
            "market": classify_market(
                description, category, permit_type, work_type
            ),
            "work_class": work_type or "Unknown",
            "proposed_use": category or "Unknown",
            "status": status or "Active",
            "value": format_money(value_numeric),
            "value_numeric": value_numeric,
            "permit_number": permit_number,
            "issued_date": (
                issued.strftime("%Y-%m-%dT%H:%M:%S.000")
                if issued else "Unknown"
            ),
            "description": description,
            "contractor": contractor or "Unknown",
            "contractors": [contractor] if contractor else [],
            "source": LOUISVILLE_SOURCE_LABEL,
            "source_url": LOUISVILLE_PORTAL_URL,
            "_issued": issued,
        })

    for reason, count in skipped.items():
        print(f"  Skipped {count} records ({reason}).")

    recent = [
        project for project in projects
        if within_lookback(project["_issued"])
    ]

    if recent:
        print(
            f"Kept {len(recent)} Louisville projects "
            f"issued in the last {DAYS_BACK} days."
        )
        kept = recent
    elif projects:
        print(
            f"WARNING: no permits within the last {DAYS_BACK} days - "
            f"this layer may be a point-in-time snapshot. "
            f"Keeping the {FALLBACK_KEEP} highest-value permits instead."
        )
        projects.sort(
            key=lambda item: item["value_numeric"],
            reverse=True,
        )
        kept = projects[:FALLBACK_KEEP]
    else:
        print(
            "WARNING: zero Louisville projects survived filtering. "
            "Check the skip counts above - if most records were "
            "'below min value', the layer's cost field may be empty."
        )
        kept = []

    for project in kept:
        project.pop("_issued", None)

    return kept


# ============================================================
# SOURCE 2: CSV DROP-IN (Lexington / statewide / other counties)
# ============================================================

CSV_COLUMNS = [
    "permit_number", "project", "address", "city", "county",
    "status", "value", "contractor", "description", "work_class",
    "issued_date", "latitude", "longitude", "source", "source_url",
]


def build_csv_projects():

    projects = []
    pattern = os.path.join(CSV_SOURCE_FOLDER, "*.csv")

    for path in sorted(glob.glob(pattern)):
        print(f"Reading CSV source: {path}")

        with open(path, "r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)

            for row in reader:
                row = {
                    clean_text(key).lower(): clean_text(value)
                    for key, value in row.items()
                    if key
                }

                value_numeric = parse_money(row.get("value"))
                description = row.get("description", "")
                work_class = row.get("work_class", "")
                issued = parse_date(row.get("issued_date"))

                if is_excluded(f"{description} {work_class}"):
                    continue
                if value_numeric and value_numeric < MIN_VALUE:
                    continue
                if not within_lookback(issued):
                    continue

                address = row.get("address", "")
                project_name = (
                    row.get("project")
                    or (f"{description} - {address}"
                        if description and address
                        else address or "Project")
                )

                latitude = row.get("latitude")
                longitude = row.get("longitude")

                projects.append({
                    "project": project_name,
                    "address": address,
                    "city": row.get("city", ""),
                    "county": row.get("county", "Unknown"),
                    "state": "KY",
                    "zipcode": row.get("zipcode", ""),
                    "neighborhood": "",
                    "square_feet": None,
                    "latitude": (
                        float(latitude) if latitude else None
                    ),
                    "longitude": (
                        float(longitude) if longitude else None
                    ),
                    "type": row.get("type", "Building"),
                    "market": classify_market(
                        description,
                        row.get("proposed_use", ""),
                        row.get("type", ""),
                        work_class,
                    ),
                    "work_class": work_class or "Unknown",
                    "proposed_use": row.get("proposed_use", "Unknown"),
                    "status": row.get("status", "Unknown"),
                    "value": format_money(value_numeric),
                    "value_numeric": value_numeric,
                    "permit_number": row.get("permit_number", ""),
                    "issued_date": (
                        issued.strftime("%Y-%m-%dT%H:%M:%S.000")
                        if issued else "Unknown"
                    ),
                    "description": description or "Construction Permit",
                    "contractor": row.get("contractor") or "Unknown",
                    "contractors": (
                        [row["contractor"]]
                        if row.get("contractor") else []
                    ),
                    "source": row.get("source") or os.path.basename(path),
                    "source_url": row.get("source_url", ""),
                })

    if projects:
        print(f"Kept {len(projects)} projects from CSV sources.")

    return projects


# ============================================================
# MAIN
# ============================================================

def main():

    projects = []
    projects.extend(build_louisville_projects())
    projects.extend(build_csv_projects())

    # De-duplicate by permit number (keep first occurrence)
    seen = set()
    unique_projects = []

    for project in projects:
        key = (
            project.get("permit_number")
            or f"{project.get('address')}|{project.get('value_numeric')}"
        )
        if key in seen:
            continue
        seen.add(key)
        unique_projects.append(project)

    projects = unique_projects

    # Geocode anything missing coordinates
    cache = load_geocode_cache()
    geocoded = 0

    for project in projects:
        if project["latitude"] is None or project["longitude"] is None:
            latitude, longitude = geocode_address(
                project["address"],
                project["city"],
                project["state"],
                cache,
            )
            project["latitude"] = latitude
            project["longitude"] = longitude
            geocoded += 1

    save_geocode_cache(cache)

    if geocoded:
        print(f"Geocoded {geocoded} addresses.")

    # Score every project: dollars (0-5) + proximity to branch (0-5)
    for project in projects:
        miles = distance_miles(
            project["latitude"],
            project["longitude"],
        )
        project["distance_miles"] = miles
        project["distance"] = (
            f"{miles} mi" if miles is not None else "Unknown"
        )

        score, reason = opportunity_score(
            project["value_numeric"],
            miles,
        )
        project["opportunity_score"] = score
        project["opportunity"] = f"{score}/10"
        project["opportunity_reason"] = reason
        project["date_discovered"] = (
            datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )

    # Best opportunities first: score, then closest, then biggest
    projects.sort(
        key=lambda item: (
            -item["opportunity_score"],
            item["distance_miles"]
            if item["distance_miles"] is not None else 9999,
            -item["value_numeric"],
        ),
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
        json.dump(projects, file, indent=2, ensure_ascii=False)

    print(f"Wrote {len(projects)} projects to {OUTPUT_FILE}.")


if __name__ == "__main__":
    main()
