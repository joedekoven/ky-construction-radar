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
import re
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
        "services/Louisville_Metro_KY_All_Permits_(Historical)/"
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
    "poolspa",
    "pool spa",
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


def safe_float(value):
    """Convert to float, or None if the value isn't numeric."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


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

    # Regex fallback: catches any remaining Y/M/D or M/D/Y style
    # (e.g. "2026/07/28 00:00:00+00") regardless of separators.
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        year, month, day = (int(g) for g in match.groups())
    else:
        match = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", text)
        if match:
            month, day, year = (int(g) for g in match.groups())
        else:
            return None

    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
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

DISCOVERY_QUERIES = [
    'Louisville Metro permits type:"Feature Service"',
    'LOJIC construction permits type:"Feature Service"',
    'Jefferson County Kentucky building permits type:"Feature Service"',
]

DISCOVERY_EXCLUDE = [
    "right of way", "right-of-way", "apcd", "air", "burn",
    "gasoline", "alcohol", "abc", "tank", "sign",
]


def discover_louisville_services():
    """
    Ask ArcGIS Online's public search API for Louisville permit
    feature services. Self-healing: if the city moves its data
    again, this finds the new home automatically.
    """

    found = []

    for query in DISCOVERY_QUERIES:
        params = urllib.parse.urlencode({
            "q": query,
            "f": "json",
            "num": "30",
            "sortField": "modified",
            "sortOrder": "desc",
        })

        try:
            data = http_get_json(
                "https://www.arcgis.com/sharing/rest/search?" + params
            )
        except Exception as error:
            print(f"  Discovery query failed: {error}")
            continue

        for item in data.get("results", []):
            url = (item.get("url") or "").rstrip("/")
            title = (item.get("title") or "").lower()
            owner = (item.get("owner") or "").lower()

            if not url or "featureserver" not in url.lower():
                continue
            if "permit" not in title:
                continue
            if any(word in title for word in DISCOVERY_EXCLUDE):
                continue
            if not any(
                word in title + " " + owner
                for word in ["louisville", "jefferson", "lojic"]
            ):
                continue

            found.append(url)

    # De-duplicate, preserve order
    seen = set()
    unique = []
    for url in found:
        if url.lower() not in seen:
            seen.add(url.lower())
            unique.append(url)

    print(f"Discovery found {len(unique)} candidate permit services.")
    return unique


def candidate_layer_urls():
    """
    Known layer URLs first, then discovered services (expanding
    each service into its individual layers).
    """

    candidates = list(LOUISVILLE_LAYER_URLS)

    try:
        for service_url in discover_louisville_services():

            # Already points at a specific layer
            if service_url.split("/")[-1].isdigit():
                candidates.append(service_url)
                continue

            # Expand the service into its layers
            try:
                info = http_get_json(service_url + "?f=json")
                for layer in (info.get("layers") or [])[:6]:
                    layer_id = layer.get("id")
                    if layer_id is not None:
                        candidates.append(
                            f"{service_url}/{layer_id}"
                        )
            except Exception:
                candidates.append(service_url + "/0")

    except Exception as error:
        print(f"  WARNING: discovery failed entirely: {error}")

    # De-duplicate, preserve order, cap the probe list
    seen = set()
    unique = []
    for url in candidates:
        if url.lower() not in seen:
            seen.add(url.lower())
            unique.append(url)

    return unique[:12]


def looks_like_permits(rows):
    """True if the rows have permit-ish and issue-date-ish fields."""
    if not rows:
        return False
    keys = " ".join(
        rows[0].get("attributes", {}).keys()
    ).lower()
    return "permit" in keys and "issue" in keys

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

    if order_field:
        print(f"  (ordering newest-first by {order_field})")
    else:
        print(
            "  (WARNING: could not read layer info - rows will "
            "arrive oldest-first, newest data may be missed)"
        )

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


def louisville_record(attributes):
    """
    Map one Louisville permit record to a project dict.
    Handles BOTH the legacy schema (PERMITNUMBER, ISSUEDATE...)
    and the new data.lojic.org schema (PERMIT_NUMBER, ISSUE_DATE...).
    Returns (project, skip_reason): exactly one is None.
    """

    permit_number = clean_text(
        get_field(
            attributes,
            "PERMIT_NUMBER", "PERMITNUMBER", "PermitNumber",
        )
    )
    permit_type = clean_text(
        get_field(
            attributes,
            "PERMIT_TYPE", "PERMITTYPE", "PermitType",
        )
    )
    category = clean_text(
        get_field(
            attributes,
            "CATEGORY_NAME", "CATEGORYNAME", "CategoryName",
        )
    )
    work_type = clean_text(
        get_field(
            attributes,
            "WORK_TYPE", "WORKTYPE", "WorkType",
        )
    )
    status = clean_text(
        get_field(
            attributes,
            "PERMIT_STATUS", "STATUS", "Status",
        )
    )
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
    square_feet = get_field(
        attributes, "SQFT", "SQUAREFEET", "SquareFeet"
    )

    value_numeric = parse_money(
        get_field(
            attributes,
            "PROJECT_COSTS", "PROJECTCOSTS", "ProjectCosts",
        )
    )

    issued = parse_date(
        get_field(
            attributes,
            "ISSUE_DATE", "ISSUEDATE", "IssueDate", "ISSUEDDATE",
        )
    )

    latitude = get_field(attributes, "LATITUDE", "Latitude", "LAT")
    longitude = get_field(
        attributes, "LONGITUDE", "Longitude", "LON", "LONG"
    )

    combined_text = " ".join([permit_type, category, work_type])

    if is_excluded(combined_text):
        return None, "excluded type"
    if value_numeric < MIN_VALUE:
        return None, "below min value"

    description = " - ".join(
        part for part in [permit_type, work_type] if part
    ) or "Construction Permit"

    title_use = category or permit_type or "Project"
    project_name = f"{title_use} - {address}" if address else title_use

    return {
        "project": project_name,
        "address": address,
        "city": city,
        "county": "Jefferson",
        "state": "KY",
        "zipcode": zipcode,
        "neighborhood": neighborhood,
        "square_feet": square_feet,
        "latitude": safe_float(latitude),
        "longitude": safe_float(longitude),
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
    }, None


def build_louisville_projects():

    def newest_issue_date(rows):
        """Most recent parseable issue date in the rows, or None."""
        newest = None
        for row in rows:
            issued = parse_date(
                get_field(
                    row.get("attributes", {}),
                    "ISSUE_DATE", "ISSUEDATE",
                    "IssueDate", "ISSUEDDATE",
                )
            )
            if issued and (newest is None or issued > newest):
                newest = issued
        return newest

    # Probe every candidate with a cheap 1000-row sample
    # (newest-first), then fully download only the freshest one.
    best_newest = None
    best_url = None
    best_rows = []

    for layer_url in candidate_layer_urls():
        try:
            print(f"Probing layer:\n  {layer_url}")
            rows = fetch_arcgis_features(
                layer_url, max_records=1000
            )

            if not rows:
                print("  ...no rows.")
                continue

            if not looks_like_permits(rows):
                print("  ...doesn't look like permit data, skipping.")
                continue

            newest = newest_issue_date(rows)
            print(
                f"  ...{len(rows)} rows sampled, newest permit: "
                f"{newest.date() if newest else 'UNPARSEABLE'}"
            )

            if newest and (best_newest is None or newest > best_newest):
                best_newest = newest
                best_url = layer_url
                best_rows = rows

            # A feed current within the last week is the live one -
            # stop probing.
            age_days = (
                (datetime.now(timezone.utc) - newest).days
                if newest else 9999
            )
            if age_days <= 7:
                print("  ...fresh feed found - using this layer.")
                break

        except Exception as error:
            print(f"  WARNING: probe failed ({error}).")

    features = []

    if best_url:
        print(
            f"Best source (newest permit "
            f"{best_newest.date() if best_newest else 'unknown'}):\n"
            f"  {best_url}"
        )
        if within_lookback(best_newest):
            print("Downloading full dataset from best source...")
            try:
                features = fetch_arcgis_features(
                    best_url, max_records=20_000
                )
            except Exception as error:
                print(f"  Full download failed ({error}); "
                      f"using probe sample.")
                features = best_rows
        else:
            print(
                "WARNING: even the best source is stale - "
                "using its sample so the dashboard isn't empty."
            )
            features = best_rows

    print(f"Using {len(features)} Louisville permit records.")

    projects = []
    skipped = {"excluded type": 0, "below min value": 0}

    for feature in features:
        attributes = feature.get("attributes", {})

        project, skip_reason = louisville_record(attributes)

        if skip_reason:
            skipped[skip_reason] = skipped.get(skip_reason, 0) + 1
            continue

        projects.append(project)

    for reason, count in skipped.items():
        print(f"  Skipped {count} records ({reason}).")

    recent = [
        project for project in projects
        if within_lookback(project["_issued"])
    ]

    print(
        f"{len(recent)} Louisville projects issued "
        f"in the last {DAYS_BACK} days."
    )

    if len(recent) >= 25:
        kept = recent
    elif projects:
        # Too few recent permits (stale feed or slow period):
        # top up with the highest-value permits regardless of date
        # so the dashboard is always useful.
        print(
            f"Fewer than 25 recent permits - topping up with the "
            f"highest-value permits (up to {FALLBACK_KEEP} total)."
        )
        recent_keys = {id(project) for project in recent}
        others = [
            project for project in projects
            if id(project) not in recent_keys
        ]
        others.sort(
            key=lambda item: item["value_numeric"],
            reverse=True,
        )
        kept = recent + others[: max(FALLBACK_KEEP - len(recent), 0)]
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

            fieldnames = [
                clean_text(name).upper()
                for name in (reader.fieldnames or [])
            ]

            # Raw export straight from data.lojic.org? Route it
            # through the Louisville mapper - no reformatting needed.
            if (
                "PERMIT_NUMBER" in fieldnames
                and "ISSUE_DATE" in fieldnames
            ):
                print(
                    "  ...detected raw Louisville/LOJIC export - "
                    "mapping automatically."
                )

                skipped = {}
                raw_kept = []

                for row in reader:
                    project, skip_reason = louisville_record(row)

                    if skip_reason:
                        skipped[skip_reason] = (
                            skipped.get(skip_reason, 0) + 1
                        )
                        continue

                    if not within_lookback(project["_issued"]):
                        skipped["outside lookback"] = (
                            skipped.get("outside lookback", 0) + 1
                        )
                        continue

                    project.pop("_issued", None)
                    raw_kept.append(project)

                for reason, count in skipped.items():
                    print(f"  Skipped {count} rows ({reason}).")

                print(
                    f"  Kept {len(raw_kept)} projects from "
                    f"this export."
                )
                projects.extend(raw_kept)
                continue

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
                    "latitude": safe_float(latitude),
                    "longitude": safe_float(longitude),
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

    try:
        projects.extend(build_louisville_projects())
    except Exception as error:
        print(f"ERROR: Louisville source failed entirely: {error}")

    try:
        projects.extend(build_csv_projects())
    except Exception as error:
        print(f"ERROR: CSV sources failed: {error}")

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
