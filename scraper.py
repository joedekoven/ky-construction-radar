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
]

LOUISVILLE_SOURCE_LABEL = "Louisville Metro Construction Review"

LOUISVILLE_PORTAL_URL = (
    "https://louisvilleky.gov/government/construction-review/"
    "services/online-permit-search"
)

# How far back we look for permits (by issue date)
DAYS_BACK = 90

# Ignore most tiny projects
MIN_VALUE = 25_000

GEOCODE_CACHE_FILE = "geocode_cache.json"

OUTPUT_FILE = "projects.json"

CSV_SOURCE_FOLDER = "sources"


EXCLUDE_KEYWORDS = [
    "single family",
    "single-family",
    "sngl fam",
    "two family",
    "2 family",
    "duplex",
    "deck",
    "fence",
    "swimming pool",
    "above ground pool",
    "inground pool",
    "residential garage",
    "detached garage",
    "shed",
    "carport",
    "mobile home",
    "manufactured home",
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
        "senior living", "r-1", "r-2", "r-3", "1-2-3 fm",
    ]):
        return "Multifamily / Residential"

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
# ELECTRICAL OPPORTUNITY SCORE
# ============================================================

def electrical_score(description, proposed_use, workclass, value):

    text = " ".join([
        clean_text(description),
        clean_text(proposed_use),
        clean_text(workclass),
    ]).lower()

    score = 3
    reasons = []

    if value >= 20_000_000:
        score += 4
        reasons.append("very large construction value")
    elif value >= 5_000_000:
        score += 3
        reasons.append("large construction value")
    elif value >= 1_000_000:
        score += 2
        reasons.append("significant construction value")
    elif value >= 250_000:
        score += 1
        reasons.append("meaningful commercial project value")

    very_high_types = [
        "hospital", "manufacturing", "factory",
        "data center", "industrial", "distillery",
    ]

    high_types = [
        "school", "university", "apartment", "multifamily",
        "hotel", "warehouse", "mixed use", "mixed-use",
        "distribution",
    ]

    if any(word in text for word in very_high_types):
        score += 3
        reasons.append("electrically intensive project type")
    elif any(word in text for word in high_types):
        score += 2
        reasons.append("strong potential electrical material demand")

    if "new" in text or "addition" in text:
        score += 1
        reasons.append("new construction or expansion")

    score = min(max(score, 1), 10)

    if not reasons:
        reasons.append(
            "potential commercial electrical material opportunity"
        )

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

def fetch_arcgis_features(layer_url):
    """Page through an ArcGIS feature layer and return all rows."""

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
        if offset > 50_000:
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

    features = []
    used_url = None

    for layer_url in LOUISVILLE_LAYER_URLS:
        try:
            print(f"Downloading Louisville permits from:\n  {layer_url}")
            features = fetch_arcgis_features(layer_url)
            if features:
                used_url = layer_url
                break
            print("  ...layer returned no rows, trying next URL.")
        except Exception as error:
            print(f"  WARNING: layer failed ({error}), trying next URL.")

    print(f"Downloaded {len(features)} Louisville permit records.")

    projects = []

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

        # Filters
        if is_excluded(combined_text):
            continue
        if value_numeric < MIN_VALUE:
            continue
        if not within_lookback(issued):
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
        })

    print(
        f"Kept {len(projects)} Louisville projects "
        f"after filtering."
    )

    return projects


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

    # Score every project
    for project in projects:
        score, reason = electrical_score(
            project["description"],
            project["proposed_use"],
            project["work_class"],
            project["value_numeric"],
        )
        project["opportunity_score"] = score
        project["opportunity"] = f"{score}/10"
        project["opportunity_reason"] = reason
        project["date_discovered"] = (
            datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )

    # Highest value opportunities first
    projects.sort(
        key=lambda item: (
            item["opportunity_score"],
            item["value_numeric"],
        ),
        reverse=True,
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
        json.dump(projects, file, indent=2, ensure_ascii=False)

    print(f"Wrote {len(projects)} projects to {OUTPUT_FILE}.")


if __name__ == "__main__":
    main()
