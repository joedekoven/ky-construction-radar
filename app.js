let allProjects = [];
let visibleProjects = [];
let mapMarkers = [];

let sortKey = null;
let sortDirection = "desc";


/* =========================================================
   SORTING
========================================================= */

const SORT_COLUMNS = {
    project: {
        get: project => (project.project || "").toLowerCase(),
        type: "string",
        firstClick: "asc"
    },
    market: {
        get: project => (project.market || "").toLowerCase(),
        type: "string",
        firstClick: "asc"
    },
    county: {
        get: project => (project.county || "").toLowerCase(),
        type: "string",
        firstClick: "asc"
    },
    issued: {
        get: project => {
            const time = Date.parse(project.issued_date);
            return Number.isNaN(time) ? null : time;
        },
        type: "number",
        firstClick: "desc"
    },
    status: {
        get: project => (project.status || "").toLowerCase(),
        type: "string",
        firstClick: "asc"
    },
    value: {
        get: project => project.value_numeric ?? null,
        type: "number",
        firstClick: "desc"
    },
    distance: {
        get: project => project.distance_miles ?? null,
        type: "number",
        firstClick: "asc"
    },
    contractor: {
        get: project => (project.contractor || "").toLowerCase(),
        type: "string",
        firstClick: "asc"
    },
    opportunity: {
        get: project => project.opportunity_score ?? null,
        type: "number",
        firstClick: "desc"
    }
};


function sortProjects(projects) {

    if (!sortKey || !SORT_COLUMNS[sortKey]) {
        return projects;
    }

    const column = SORT_COLUMNS[sortKey];
    const direction = sortDirection === "asc" ? 1 : -1;

    return [...projects].sort((a, b) => {

        const valueA = column.get(a);
        const valueB = column.get(b);

        // Missing values always sink to the bottom
        const missingA =
            valueA === null || valueA === "";
        const missingB =
            valueB === null || valueB === "";

        if (missingA && missingB) return 0;
        if (missingA) return 1;
        if (missingB) return -1;

        if (column.type === "number") {
            return (valueA - valueB) * direction;
        }

        return String(valueA)
            .localeCompare(String(valueB)) * direction;
    });
}


function updateSortIndicators() {

    document
        .querySelectorAll("th.sortable")
        .forEach(header => {

            header.classList.remove(
                "sorted-asc",
                "sorted-desc"
            );

            if (header.dataset.sort === sortKey) {
                header.classList.add(
                    sortDirection === "asc"
                        ? "sorted-asc"
                        : "sorted-desc"
                );
            }
        });
}


/* =========================================================
   MAP SETUP
========================================================= */

const constructionMap = L.map(
    "constructionMap",
    {
        zoomControl: true
    }
).setView(
    [37.8393, -85.4],
    8
);


L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    {
        attribution:
            "&copy; OpenStreetMap contributors &copy; CARTO",
        subdomains: "abcd",
        maxZoom: 20
    }
).addTo(constructionMap);


/* =========================================================
   LOAD PROJECT DATA
========================================================= */

async function loadProjects() {

    try {

        const response = await fetch("projects.json");

        if (!response.ok) {
            throw new Error("Could not load projects.json");
        }

        allProjects = await response.json();
        visibleProjects = allProjects;

        populateFilters();
        applyFilters();
        updateFreshnessStamp(allProjects);

    } catch (error) {

        console.error(error);

        document.getElementById("emptyMessage").style.display =
            "block";

        document.getElementById("emptyMessage").innerText =
            "Unable to load project data.";

    }
}


/* =========================================================
   FILTER OPTIONS
========================================================= */

function populateFilters() {

    const markets = [
        ...new Set(
            allProjects
                .map(project => project.market)
                .filter(Boolean)
        )
    ].sort();


    const counties = [
        ...new Set(
            allProjects
                .map(project => project.county)
                .filter(Boolean)
        )
    ].sort();


    const statuses = [
        ...new Set(
            allProjects
                .map(project => project.status)
                .filter(Boolean)
        )
    ].sort();


    addOptions(
        "marketFilter",
        markets
    );

    addOptions(
        "countyFilter",
        counties
    );

    addOptions(
        "statusFilter",
        statuses
    );
}


function addOptions(
    selectId,
    values
) {

    const select =
        document.getElementById(selectId);


    values.forEach(value => {

        const option =
            document.createElement("option");

        option.value = value;
        option.textContent = value;

        select.appendChild(option);

    });
}


/* =========================================================
   SUMMARY CARDS
========================================================= */

function updateSummary(projects) {

    document
        .getElementById("totalProjects")
        .innerText = projects.length;


    const residentialMarkets = [
        "Residential",
        "Multifamily",
        "Multifamily / Residential"
    ];

    const commercialMarkets = [
        "Commercial",
        "Industrial",
        "Warehouse / Logistics",
        "Mixed-Use"
    ];


    document
        .getElementById("residentialProjects")
        .innerText = projects.filter(
            project =>
                residentialMarkets.includes(project.market)
        ).length;


    document
        .getElementById("commercialProjects")
        .innerText = projects.filter(
            project =>
                commercialMarkets.includes(project.market)
        ).length;


    document
        .getElementById("highOpportunityProjects")
        .innerText = projects.filter(
            project =>
                Number(
                    project.opportunity_score || 0
                ) >= 8
        ).length;
}


/* =========================================================
   PROJECT TABLE
========================================================= */

function formatIssuedDate(raw) {

    if (!raw || raw === "Unknown") {
        return "Unknown";
    }

    const parsed = new Date(raw);

    if (Number.isNaN(parsed.getTime())) {
        return raw;
    }

    return parsed.toLocaleDateString(
        "en-US",
        { month: "short", day: "numeric", year: "numeric" }
    );
}


function exportVisibleToCsv() {

    const rows = currentlyVisibleProjects();

    if (!rows.length) {
        return;
    }

    const columns = [
        ["Permit #", project => project.permit_number],
        ["Project", project => project.project],
        ["Address", project => project.address],
        ["City", project => project.city],
        ["County", project => project.county],
        ["Market", project => project.market],
        ["Status", project => project.status],
        ["Issued", project => formatIssuedDate(project.issued_date)],
        ["Value", project => project.value],
        ["Distance", project => project.distance],
        ["Contractor", project => project.contractor],
        ["Opportunity", project => project.opportunity],
        ["Why", project => project.opportunity_reason],
    ];

    const csvEscape = value => {
        const text = String(value ?? "");
        return `"${text.replace(/"/g, '""')}"`;
    };

    const lines = [
        columns.map(column => csvEscape(column[0])).join(",")
    ];

    rows.forEach(project => {
        lines.push(
            columns
                .map(column => csvEscape(column[1](project)))
                .join(",")
        );
    });

    const blob = new Blob(
        [lines.join("\r\n")],
        { type: "text/csv;charset=utf-8;" }
    );

    const link = document.createElement("a");

    const stamp = new Date()
        .toISOString()
        .slice(0, 10);

    link.href = URL.createObjectURL(blob);
    link.download = `ky-construction-radar-${stamp}.csv`;
    link.click();

    URL.revokeObjectURL(link.href);
}

function renderProjects(projects) {

    const tableBody =
        document.getElementById(
            "projectsTableBody"
        );

    const emptyMessage =
        document.getElementById(
            "emptyMessage"
        );


    tableBody.innerHTML = "";


    if (projects.length === 0) {

        emptyMessage.style.display =
            "block";

        return;

    }


    emptyMessage.style.display =
        "none";


    projects.forEach(project => {

        const row =
            document.createElement("tr");


        row.innerHTML = `
            <td>
                ${escapeHtml(
                    project.project || "Unknown"
                )}
                <div class="permit-sub">
                    ${escapeHtml(
                        project.permit_number || ""
                    )}
                </div>
            </td>

            <td>
                ${escapeHtml(
                    project.market || "Other"
                )}
            </td>

            <td>
                ${escapeHtml(
                    project.county || "Unknown"
                )}
            </td>

            <td>
                ${escapeHtml(
                    formatIssuedDate(project.issued_date)
                )}
            </td>

            <td>
                ${escapeHtml(
                    project.status || "Unknown"
                )}
            </td>

            <td>
                ${escapeHtml(
                    project.value || "Unknown"
                )}
            </td>

            <td>
                ${escapeHtml(
                    project.distance || "Unknown"
                )}
            </td>

            <td>
                ${escapeHtml(
                    project.contractor || "Unknown"
                )}
            </td>

            <td class="${
                Number(
                    project.opportunity_score || 0
                ) >= 8
                    ? "score-high"
                    : ""
            }">
                ${escapeHtml(
                    project.opportunity || "Unknown"
                )}
            </td>
        `;


        row.addEventListener(
            "click",
            () => focusProjectOnMap(project)
        );


        tableBody.appendChild(row);

    });
}


/* =========================================================
   MAP MARKERS
========================================================= */

function renderMapMarkers(projects) {

    clearMapMarkers();


    const markerCoordinates = [];


    projects.forEach(project => {

      if (
          project.latitude === null ||
          project.longitude === null ||
          project.latitude === undefined ||
          project.longitude === undefined ||
          project.latitude === "" ||
          project.longitude === ""
      ) {
          return;
      }

const latitude =
    Number(project.latitude);

const longitude =
    Number(project.longitude);


if (
    !Number.isFinite(latitude) ||
    !Number.isFinite(longitude) ||
    latitude < 36.3 ||
    latitude > 39.3 ||
    longitude < -89.7 ||
    longitude > -81.8
) {
    return;
}


        const opportunityScore =
            Number(
                project.opportunity_score || 0
            );


        const markerColor =
            getMarkerColor(opportunityScore);


        const marker = L.circleMarker(
            [latitude, longitude],
            {
                radius:
                    opportunityScore >= 8
                        ? 9
                        : opportunityScore >= 5
                            ? 7
                            : 6,

                color: markerColor,
                fillColor: markerColor,
                fillOpacity: 0.82,

                weight: 2,
                opacity: 1
            }
        );


        marker.bindPopup(
            createProjectPopup(project),
            {
                maxWidth: 330,
                className:
                    "construction-popup"
            }
        );


        marker.addTo(constructionMap);


        marker.projectPermitNumber =
            project.permit_number;


        mapMarkers.push(marker);

        markerCoordinates.push(
            [latitude, longitude]
        );

    });


    if (markerCoordinates.length > 0) {

        const bounds =
            L.latLngBounds(
                markerCoordinates
            );


        constructionMap.fitBounds(
            bounds,
            {
                padding: [35, 35],
                maxZoom: 13
            }
        );

    }
}


function clearMapMarkers() {

    mapMarkers.forEach(marker => {
        constructionMap.removeLayer(marker);
    });

    mapMarkers = [];
}


function getMarkerColor(score) {

    if (score >= 8) {
        return "#f0b429";
    }

    if (score >= 5) {
        return "#6ea8ff";
    }

    return "#5b6478";
}


function createProjectPopup(project) {

    const score =
        Number(
            project.opportunity_score || 0
        );


    const sourceButton =
        project.source_url
            ? `
                <a
                    href="${escapeHtml(
                        project.source_url
                    )}"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="popup-source-link"
                >
                    View Original Permit ↗
                </a>
            `
            : "";


    return `
        <div class="project-popup">

            <div class="popup-label">
                Construction Opportunity
            </div>

            <div class="popup-title">
                ${escapeHtml(
                    project.project || "Unknown Project"
                )}
            </div>

            <div class="popup-address">
                ${escapeHtml(
                    project.address || "Unknown Address"
                )},
                ${escapeHtml(
                    project.city || ""
                )}
            </div>

            <div class="popup-grid">

                <div>
                    <span>Market</span>
                    <strong>
                        ${escapeHtml(
                            project.market || "Unknown"
                        )}
                    </strong>
                </div>

                <div>
                    <span>Value</span>
                    <strong>
                        ${escapeHtml(
                            project.value || "Unknown"
                        )}
                    </strong>
                </div>

                <div>
                    <span>Contractor</span>
                    <strong>
                        ${escapeHtml(
                            project.contractor || "Unknown"
                        )}
                    </strong>
                </div>

                <div>
                    <span>Distance</span>
                    <strong>
                        ${escapeHtml(
                            project.distance || "Unknown"
                        )}
                    </strong>
                </div>

                <div>
                    <span>Permit #</span>
                    <strong>
                        ${escapeHtml(
                            project.permit_number || "Unknown"
                        )}
                    </strong>
                </div>

                <div>
                    <span>Issued</span>
                    <strong>
                        ${escapeHtml(
                            formatIssuedDate(project.issued_date)
                        )}
                    </strong>
                </div>

            </div>

            <div class="popup-opportunity">
                <span>Opportunity Score</span>

                <strong>
                    ${score}/10
                </strong>
            </div>

            <div class="popup-reason">
                ${escapeHtml(
                    project.opportunity_reason ||
                    "Potential construction opportunity."
                )}
            </div>

            <a
                class="popup-source-link"
                href="https://www.google.com/maps/dir/?api=1&destination=${
                    encodeURIComponent(
                        (project.address || "") + ", " +
                        (project.city || "") + ", KY"
                    )
                }"
                target="_blank"
                rel="noopener"
            >
                Directions &rarr;
            </a>
            &nbsp;&nbsp;
            ${sourceButton}

        </div>
    `;
}


function focusProjectOnMap(project) {

    const latitude =
        Number(project.latitude);

    const longitude =
        Number(project.longitude);


    if (
        !Number.isFinite(latitude) ||
        !Number.isFinite(longitude)
    ) {
        return;
    }


    constructionMap.flyTo(
        [latitude, longitude],
        15,
        {
            duration: 0.8
        }
    );


    const marker =
        mapMarkers.find(
            currentMarker =>
                currentMarker.projectPermitNumber ===
                project.permit_number
        );


    if (marker) {
        marker.openPopup();
    }
}


/* =========================================================
   FILTERING
========================================================= */

function applyFilters() {

    const search =
        document
            .getElementById("searchInput")
            .value
            .toLowerCase();


    const market =
        document
            .getElementById("marketFilter")
            .value;


    const county =
        document
            .getElementById("countyFilter")
            .value;


    const status =
        document
            .getElementById("statusFilter")
            .value;

    const minValue = Number(
        document
            .getElementById("valueFilter")
            .value
    ) || 0;

    const maxDistance = Number(
        document
            .getElementById("distanceFilter")
            .value
    ) || 0;

    const issuedDays = Number(
        document
            .getElementById("issuedFilter")
            .value
    ) || 0;

    const issuedCutoff =
        issuedDays > 0
            ? Date.now() - issuedDays * 86400000
            : 0;


    const filtered =
        allProjects.filter(project => {

            const searchableText = `
                ${project.project || ""}
                ${project.address || ""}
                ${project.city || ""}
                ${project.description || ""}
                ${project.contractor || ""}
                ${project.permit_number || ""}
            `.toLowerCase();


            const matchesSearch =
                searchableText.includes(search);


            const matchesMarket =
                !market ||
                project.market === market;


            const matchesCounty =
                !county ||
                project.county === county;


            const matchesStatus =
                !status ||
                project.status === status;


            const matchesValue =
                !minValue ||
                (project.value_numeric || 0) >= minValue;


            const matchesDistance =
                !maxDistance ||
                (
                    project.distance_miles !== null &&
                    project.distance_miles !== undefined &&
                    project.distance_miles <= maxDistance
                );


            let matchesIssued = true;

            if (issuedCutoff) {
                const issuedTime =
                    Date.parse(project.issued_date);

                matchesIssued =
                    !Number.isNaN(issuedTime) &&
                    issuedTime >= issuedCutoff;
            }


            return (
                matchesSearch &&
                matchesMarket &&
                matchesCounty &&
                matchesStatus &&
                matchesValue &&
                matchesDistance &&
                matchesIssued
            );

        });


    const sorted = sortProjects(filtered);

    visibleProjects = sorted;

    updateSummary(sorted);
    renderProjects(sorted);
    renderMapMarkers(sorted);
    updateSortIndicators();
}


function currentlyVisibleProjects() {
    return visibleProjects.length
        ? visibleProjects
        : allProjects;
}


/* =========================================================
   HTML SAFETY
========================================================= */

function escapeHtml(value) {

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


/* =========================================================
   EVENT LISTENERS
========================================================= */

document
    .getElementById("searchInput")
    .addEventListener(
        "input",
        applyFilters
    );


document
    .getElementById("marketFilter")
    .addEventListener(
        "change",
        applyFilters
    );


document
    .getElementById("countyFilter")
    .addEventListener(
        "change",
        applyFilters
    );


document
    .getElementById("statusFilter")
    .addEventListener(
        "change",
        applyFilters
    );


["valueFilter", "distanceFilter", "issuedFilter"]
    .forEach(id => {
        document
            .getElementById(id)
            .addEventListener("change", applyFilters);
    });


document
    .querySelectorAll("th.sortable")
    .forEach(header => {

        header.addEventListener("click", () => {

            const key = header.dataset.sort;

            if (sortKey === key) {
                // Same column: flip direction
                sortDirection =
                    sortDirection === "asc"
                        ? "desc"
                        : "asc";
            } else {
                sortKey = key;
                sortDirection =
                    SORT_COLUMNS[key]?.firstClick || "asc";
            }

            applyFilters();
        });
    });


function updateFreshnessStamp(projects) {

    const stampElement =
        document.getElementById("dataUpdated");

    if (!stampElement || !projects.length) {
        return;
    }

    const newest = projects
        .map(project => project.date_discovered)
        .filter(Boolean)
        .sort()
        .pop();

    if (newest) {
        stampElement.innerText =
            `Data updated ${formatIssuedDate(newest)}`;
    }
}


const exportButton =
    document.getElementById("exportCsv");

if (exportButton) {
    exportButton.addEventListener(
        "click",
        exportVisibleToCsv
    );
}


loadProjects();
