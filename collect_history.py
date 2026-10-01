import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import truststore

truststore.inject_into_ssl()

import requests
from requests_negotiate_sspi import HttpNegotiateAuth


SITE_URL = "https://intranet.us.avl.com/TSI/CustomerServices/test"
LIST_PATH = "/TSI/CustomerServices/test/Lists/Gas Bottle Room Utilization"
VIEW_ID = "51aae9a2-56f9-4ef3-aab4-0336073428dc"
DATABASE_PATH = Path(__file__).with_name("gas_bottle_history.db")

VERSION_FIELDS = {
    "span_line": "Title",
    "concentration": "Concentration",
    "concentration_2nd": "Concentration_x005f_x0020_x005f_Balance",
    "balance": "Balance",
    "cylinder_number": (
        "Cyl_x005f_x002e_x005f__x005f_x0020_x005f_Number"
    ),
    "online_date": "Online_x005f_x0020_x005f_date",
    "expiration_date": (
        "Exp_x005f_x002e_x005f__x005f_x0020_x005f_Date"
    ),
    "gas_name": "Gas_x005f_x0020_x005f_Name",
    "concentration_unit": "Concentration_x005f_x0020_x005f_Unit",
    "concentration_2nd_unit": (
        "Concentration2nd_x005f_x0020_x005f_Unit"
    ),
}


def _check_response(response: requests.Response) -> None:
    if response.status_code in (401, 403):
        raise RuntimeError(
            "SharePoint rejected Windows authentication. Run this script while "
            "signed in to an AVL Windows account and connected to the AVL network or VPN."
        )
    response.raise_for_status()


def _list_data(payload: dict) -> dict:
    data = payload.get("ListData", payload)
    return json.loads(data) if isinstance(data, str) else data


def _fetch_current_items(session: requests.Session) -> list[dict]:
    url = f"{SITE_URL}/_api/web/GetList(@listUrl)/RenderListDataAsStream"
    params = {"@listUrl": f"'{LIST_PATH}'", "View": VIEW_ID}
    rows = []
    seen_pages = set()

    while True:
        response = session.post(
            url,
            params=params,
            json={
                "parameters": {
                    "AddRequiredFields": "true",
                    "DatesInUtc": "true",
                    "RenderOptions": 2,
                }
            },
            timeout=30,
        )
        _check_response(response)
        data = _list_data(response.json())
        page_rows = data.get("Row")
        if not isinstance(page_rows, list):
            raise ValueError("SharePoint did not return a list of current items.")
        rows.extend(page_rows)

        next_href = data.get("NextHref")
        if not next_href:
            return list({row["ID"]: row for row in rows}.values())
        if next_href in seen_pages:
            raise ValueError("SharePoint returned a repeated current-item paging link.")
        seen_pages.add(next_href)
        paging = dict(
            parse_qsl(
                urlsplit(unescape(next_href)).query,
                keep_blank_values=True,
            )
        )
        params = {
            "@listUrl": f"'{LIST_PATH}'",
            "View": VIEW_ID,
            **paging,
        }


def _fetch_versions(session: requests.Session, item_id: int) -> list[dict]:
    url = f"{SITE_URL}/_api/web/GetList(@listUrl)/items({item_id})/versions"
    params = {"@listUrl": f"'{LIST_PATH}'"}
    versions = []
    seen_pages = set()

    while url:
        response = session.get(url, params=params, timeout=30)
        _check_response(response)
        payload = response.json()
        page_versions = payload.get("value")
        if not isinstance(page_versions, list):
            raise ValueError(
                f"SharePoint did not return version history for item {item_id}."
            )
        versions.extend(page_versions)

        next_url = (
            payload.get("@odata.nextLink")
            or payload.get("odata.nextLink")
            or payload.get("__next")
        )
        if not next_url:
            break
        if next_url in seen_pages:
            raise ValueError(
                f"SharePoint returned a repeated version paging link for item {item_id}."
            )
        seen_pages.add(next_url)
        url = next_url
        params = None

    return versions


def fetch_all_history() -> tuple[list[dict], list[dict]]:
    with requests.Session() as session:
        session.auth = HttpNegotiateAuth()
        session.headers.update(
            {
                "Accept": "application/json;odata=nometadata",
                "Content-Type": "application/json;odata=nometadata",
            }
        )
        items = _fetch_current_items(session)
        history = []
        for item in items:
            item_id = int(item["ID"])
            for version in _fetch_versions(session, item_id):
                history.append(
                    {
                        "sharepoint_item_id": item_id,
                        "version": version,
                    }
                )
    return items, history


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _version_row(item_id: int, version: dict) -> tuple:
    editor = version.get("Editor") or {}
    values = [
        _text(version.get(source_name))
        for source_name in VERSION_FIELDS.values()
    ]
    return (
        item_id,
        int(version["VersionId"]),
        _text(version.get("VersionLabel")),
        _text(version.get("Created")),
        int(bool(version.get("IsCurrentVersion"))),
        *values,
        _text(editor.get("LookupValue")),
        json.dumps(version, ensure_ascii=False, sort_keys=True),
    )


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


HISTORY_COLUMNS = (
    "sharepoint_item_id",
    "version_id",
    "version_label",
    "version_created_at",
    "is_current_version",
    "span_line",
    "concentration",
    "concentration_2nd",
    "balance",
    "cylinder_number",
    "online_date",
    "expiration_date",
    "gas_name",
    "concentration_unit",
    "concentration_2nd_unit",
    "modified_by",
    "raw_json",
)

HISTORY_TABLE_SQL = """
    sharepoint_item_id INTEGER NOT NULL,
    version_id INTEGER NOT NULL,
    version_label TEXT NOT NULL,
    version_created_at TEXT NOT NULL,
    is_current_version INTEGER NOT NULL,
    span_line TEXT NOT NULL,
    concentration TEXT NOT NULL,
    concentration_2nd TEXT NOT NULL,
    balance TEXT NOT NULL,
    cylinder_number TEXT NOT NULL,
    online_date TEXT NOT NULL,
    expiration_date TEXT NOT NULL,
    gas_name TEXT NOT NULL,
    concentration_unit TEXT NOT NULL,
    concentration_2nd_unit TEXT NOT NULL,
    modified_by TEXT NOT NULL,
    raw_json TEXT NOT NULL
"""


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS history_versions (
            {HISTORY_TABLE_SQL},
            first_collected_at TEXT NOT NULL,
            PRIMARY KEY (sharepoint_item_id, version_id)
        );

        CREATE INDEX IF NOT EXISTS versions_by_span_line
            ON history_versions (span_line, version_created_at);
        CREATE INDEX IF NOT EXISTS versions_by_cylinder
            ON history_versions (cylinder_number);
        CREATE INDEX IF NOT EXISTS versions_by_collection
            ON history_versions (first_collected_at);

        CREATE TABLE IF NOT EXISTS tracked_items (
            sharepoint_item_id INTEGER PRIMARY KEY,
            span_line TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            is_active INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
            item_count INTEGER NOT NULL,
            fetched_version_count INTEGER NOT NULL,
            new_version_count INTEGER NOT NULL,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS report_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_key TEXT UNIQUE,
            generated_at TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT NOT NULL,
            output_file TEXT NOT NULL,
            new_version_count INTEGER NOT NULL,
            replacement_count INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cylinder_assignments (
            sharepoint_item_id INTEGER NOT NULL,
            start_version_id INTEGER NOT NULL,
            first_collected_at TEXT NOT NULL,
            span_line TEXT NOT NULL,
            cylinder_number TEXT NOT NULL,
            gas_name TEXT NOT NULL,
            concentration TEXT NOT NULL,
            concentration_2nd TEXT NOT NULL,
            balance TEXT NOT NULL,
            concentration_unit TEXT NOT NULL,
            concentration_2nd_unit TEXT NOT NULL,
            online_date TEXT NOT NULL,
            offline_date TEXT,
            days_online REAL,
            is_replacement INTEGER NOT NULL,
            is_current INTEGER NOT NULL,
            is_valid_for_metrics INTEGER NOT NULL,
            data_quality_issue TEXT,
            PRIMARY KEY (sharepoint_item_id, start_version_id)
        );
        """
    )


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


def _migrate_existing_history(
    connection: sqlite3.Connection, collected_at: str
) -> int:
    if connection.execute("SELECT 1 FROM history_versions LIMIT 1").fetchone():
        return 0
    if not _table_exists(connection, "span_line_tables"):
        return 0

    exported_at = collected_at
    if _table_exists(connection, "export_info"):
        saved_export = connection.execute(
            "SELECT value FROM export_info WHERE key = 'exported_at'"
        ).fetchone()
        if saved_export:
            exported_at = saved_export[0]
    parsed_exported_at = _parse_datetime(exported_at)
    if parsed_exported_at:
        exported_at = parsed_exported_at.isoformat()

    migrated = 0
    column_names = ", ".join(HISTORY_COLUMNS)
    for (table_name,) in connection.execute(
        "SELECT table_name FROM span_line_tables"
    ):
        if not _table_exists(connection, table_name):
            continue
        before = connection.total_changes
        connection.execute(
            f"""
            INSERT OR IGNORE INTO history_versions (
                {column_names},
                first_collected_at
            )
            SELECT {column_names}, ?
            FROM {_quote_identifier(table_name)}
            """,
            (exported_at,),
        )
        migrated += connection.total_changes - before
    return migrated


def _remove_redundant_history_tables(
    connection: sqlite3.Connection,
) -> int:
    redundant_tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name LIKE 'history_%'
              AND name != 'history_versions'
            """
        )
    }
    if _table_exists(connection, "span_line_tables"):
        redundant_tables.update(
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM span_line_tables"
            )
        )

    for table_name in redundant_tables:
        connection.execute(
            f"DROP TABLE IF EXISTS {_quote_identifier(table_name)}"
        )
    connection.execute("DROP TABLE IF EXISTS span_line_tables")
    connection.execute("DROP TABLE IF EXISTS export_info")
    return len(redundant_tables)


def _upsert_history(
    connection: sqlite3.Connection,
    history: list[dict],
    collected_at: str,
) -> int:
    existing_keys = set(
        connection.execute(
            "SELECT sharepoint_item_id, version_id FROM history_versions"
        )
    )
    rows = [
        _version_row(record["sharepoint_item_id"], record["version"])
        for record in history
    ]
    new_count = sum((row[0], row[1]) not in existing_keys for row in rows)
    column_names = ", ".join(HISTORY_COLUMNS)
    placeholders = ", ".join("?" for _ in range(len(HISTORY_COLUMNS) + 1))
    connection.executemany(
        f"""
        INSERT INTO history_versions (
            {column_names},
            first_collected_at
        ) VALUES ({placeholders})
        ON CONFLICT (sharepoint_item_id, version_id) DO NOTHING
        """,
        [(*row, collected_at) for row in rows],
    )
    return new_count


def _sync_tracked_items(
    connection: sqlite3.Connection,
    items: list[dict],
    collected_at: str,
) -> None:
    connection.execute("UPDATE tracked_items SET is_active = 0")
    connection.executemany(
        """
        INSERT INTO tracked_items (
            sharepoint_item_id,
            span_line,
            first_seen_at,
            last_seen_at,
            is_active
        ) VALUES (?, ?, ?, ?, 1)
        ON CONFLICT (sharepoint_item_id) DO UPDATE SET
            span_line = excluded.span_line,
            last_seen_at = excluded.last_seen_at,
            is_active = 1
        """,
        [
            (
                int(item["ID"]),
                _text(item.get("Title")),
                collected_at,
                collected_at,
            )
            for item in items
        ],
    )


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assignment_quality(
    cylinder_number: str,
    online_date: str,
    offline_date: str | None,
    now: datetime,
) -> tuple[float | None, int, str | None]:
    issues = []
    if cylinder_number.strip().casefold() in (
        "",
        "n/a",
        "na",
        "none",
        "not applicable",
    ):
        issues.append("placeholder cylinder number")

    online = _parse_datetime(online_date)
    offline = _parse_datetime(offline_date or "")
    if online is None:
        issues.append("invalid online date")
    elif online.year >= 2100:
        issues.append("sentinel future online date")
    elif online > now:
        issues.append("online date is in the future")

    if offline_date and offline is None:
        issues.append("invalid offline date")
    elif offline and offline.year >= 2100:
        issues.append("sentinel future offline date")
    elif offline and offline > now:
        issues.append("offline date is in the future")
    elif online and offline and offline < online:
        issues.append("offline date precedes online date")

    if issues or online is None:
        return None, 0, "; ".join(issues)
    end = offline or now
    return round((end - online).total_seconds() / 86400, 2), 1, None


def _derive_assignments(
    connection: sqlite3.Connection, now: datetime
) -> int:
    rows = connection.execute(
        """
        SELECT
            sharepoint_item_id,
            version_id,
            first_collected_at,
            span_line,
            cylinder_number,
            gas_name,
            concentration,
            concentration_2nd,
            balance,
            concentration_unit,
            concentration_2nd_unit,
            online_date
        FROM history_versions
        ORDER BY sharepoint_item_id, version_id
        """
    ).fetchall()

    by_item = defaultdict(list)
    for row in rows:
        by_item[row[0]].append(row)
    tracked_items = {
        row[0]: (bool(row[1]), row[2])
        for row in connection.execute(
            """
            SELECT sharepoint_item_id, is_active, last_seen_at
            FROM tracked_items
            """
        )
    }

    assignments = []
    for item_id, item_rows in by_item.items():
        item_assignments = []
        previous_cylinder = None
        for row in item_rows:
            normalized_cylinder = row[4].strip().casefold()
            if not item_assignments or normalized_cylinder != previous_cylinder:
                item_assignments.append(
                    {
                        "item_id": row[0],
                        "start_version_id": row[1],
                        "first_collected_at": row[2],
                        "span_line": row[3],
                        "cylinder_number": row[4],
                        "gas_name": row[5],
                        "concentration": row[6],
                        "concentration_2nd": row[7],
                        "balance": row[8],
                        "concentration_unit": row[9],
                        "concentration_2nd_unit": row[10],
                        "online_date": row[11],
                    }
                )
                previous_cylinder = normalized_cylinder
            else:
                current = item_assignments[-1]
                current.update(
                    {
                        "span_line": row[3],
                        "cylinder_number": row[4],
                        "gas_name": row[5],
                        "concentration": row[6],
                        "concentration_2nd": row[7],
                        "balance": row[8],
                        "concentration_unit": row[9],
                        "concentration_2nd_unit": row[10],
                        "online_date": row[11],
                    }
                )

        for index, assignment in enumerate(item_assignments):
            is_last = index == len(item_assignments) - 1
            is_active, last_seen_at = tracked_items.get(
                item_id, (False, item_rows[-1][2])
            )
            if not is_last:
                offline_date = item_assignments[index + 1]["online_date"]
            elif not is_active:
                offline_date = last_seen_at
            else:
                offline_date = None
            days_online, valid, issue = _assignment_quality(
                assignment["cylinder_number"],
                assignment["online_date"],
                offline_date,
                now,
            )
            assignments.append(
                (
                    assignment["item_id"],
                    assignment["start_version_id"],
                    assignment["first_collected_at"],
                    assignment["span_line"],
                    assignment["cylinder_number"],
                    assignment["gas_name"],
                    assignment["concentration"],
                    assignment["concentration_2nd"],
                    assignment["balance"],
                    assignment["concentration_unit"],
                    assignment["concentration_2nd_unit"],
                    assignment["online_date"],
                    offline_date,
                    days_online,
                    int(index > 0),
                    int(is_last and is_active),
                    valid,
                    issue,
                )
            )

    connection.execute("DELETE FROM cylinder_assignments")
    connection.executemany(
        """
        INSERT INTO cylinder_assignments (
            sharepoint_item_id,
            start_version_id,
            first_collected_at,
            span_line,
            cylinder_number,
            gas_name,
            concentration,
            concentration_2nd,
            balance,
            concentration_unit,
            concentration_2nd_unit,
            online_date,
            offline_date,
            days_online,
            is_replacement,
            is_current,
            is_valid_for_metrics,
            data_quality_issue
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        assignments,
    )
    return len(assignments)


def _record_failed_run(started_at: str, error: Exception) -> None:
    completed_at = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(DATABASE_PATH, timeout=30)) as connection:
        with connection:
            _create_schema(connection)
            connection.execute(
                """
                INSERT INTO collection_runs (
                    started_at,
                    completed_at,
                    status,
                    item_count,
                    fetched_version_count,
                    new_version_count,
                    error_message
                ) VALUES (?, ?, 'failed', 0, 0, 0, ?)
                """,
                (started_at, completed_at, str(error)),
            )


def collect_history(
    items: list[dict], history: list[dict], started_at: str
) -> dict[str, int]:
    if not items or not history:
        raise ValueError("SharePoint returned no history; the archive was not changed.")

    collected_at = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(DATABASE_PATH, timeout=30)) as connection:
        _create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            migrated_count = _migrate_existing_history(
                connection, collected_at
            )
            removed_table_count = _remove_redundant_history_tables(
                connection
            )
            new_count = _upsert_history(connection, history, collected_at)
            _sync_tracked_items(connection, items, collected_at)
            assignment_count = _derive_assignments(
                connection, datetime.now(timezone.utc)
            )
            total_versions = connection.execute(
                "SELECT COUNT(*) FROM history_versions"
            ).fetchone()[0]
            completed_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO collection_runs (
                    started_at,
                    completed_at,
                    status,
                    item_count,
                    fetched_version_count,
                    new_version_count,
                    error_message
                ) VALUES (?, ?, 'success', ?, ?, ?, NULL)
                """,
                (
                    started_at,
                    completed_at,
                    len(items),
                    len(history),
                    new_count,
                ),
            )
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        if removed_table_count:
            connection.execute("VACUUM")

    return {
        "items": len(items),
        "fetched_versions": len(history),
        "new_versions": new_count,
        "migrated_versions": migrated_count,
        "removed_tables": removed_table_count,
        "total_versions": total_versions,
        "assignments": assignment_count,
    }


def main() -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        items, history = fetch_all_history()
        result = collect_history(items, history, started_at)
    except Exception as error:
        try:
            _record_failed_run(started_at, error)
        except Exception as logging_error:
            raise ExceptionGroup(
                "Collection failed and its failure could not be recorded.",
                [error, logging_error],
            ) from error
        raise

    print(f"Database: {DATABASE_PATH}")
    print(f"Span Lines fetched: {result['items']}")
    print(
        f"Versions fetched: {result['fetched_versions']} "
        f"({result['new_versions']} new)"
    )
    if result["migrated_versions"]:
        print(f"Versions migrated: {result['migrated_versions']}")
    if result["removed_tables"]:
        print(f"Redundant history tables removed: {result['removed_tables']}")
    print(f"Archived versions: {result['total_versions']}")
    print(f"Cylinder assignments: {result['assignments']}")


if __name__ == "__main__":
    main()
