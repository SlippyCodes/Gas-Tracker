import html
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import plotly.graph_objects as go
import plotly.io as pio


DATABASE_PATH = Path(__file__).with_name("gas_bottle_history.db")
REPORTS_PATH = Path(__file__).with_name("reports")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_date(value: str | None) -> str:
    parsed = _parse_datetime(value)
    return parsed.date().isoformat() if parsed else (value or "")


def _gas_label(row: sqlite3.Row) -> str:
    gas_name = row["gas_name"].strip() or "Unknown gas"
    concentration = row["concentration"].strip()
    concentration_unit = row["concentration_unit"].strip()
    second = row["concentration_2nd"].strip()
    second_unit = row["concentration_2nd_unit"].strip()
    balance = row["balance"].strip()

    label = gas_name
    if concentration and concentration not in ("0", "0.0"):
        label += f" {concentration}{concentration_unit}"
    if second and second not in ("0", "0.0"):
        label += f" + {second}{second_unit}"
    if balance:
        label += f" / {balance}"
    return label


def _bar_figure(
    labels: list[str],
    values: list[float | int],
    x_title: str,
    y_title: str,
    horizontal: bool = False,
) -> go.Figure:
    if horizontal:
        figure = go.Figure(go.Bar(x=values, y=labels, orientation="h"))
    else:
        figure = go.Figure(go.Bar(x=labels, y=values))
    figure.update_layout(
        xaxis_title=x_title,
        yaxis_title=y_title,
        template="plotly_white",
        margin=dict(l=50, r=30, t=60, b=50),
    )
    return figure


def _table(headers: list[str], rows: list[list[object]]) -> str:
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body_html = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape('' if value is None else str(value))}</td>"
            for value in row
        )
        + "</tr>"
        for row in rows
    )
    if not rows:
        body_html = (
            f'<tr><td colspan="{len(headers)}" class="empty">No records</td></tr>'
        )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + header_html
        + "</tr></thead><tbody>"
        + body_html
        + "</tbody></table></div>"
    )


def _load_assignments(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT *
        FROM cylinder_assignments
        ORDER BY sharepoint_item_id, start_version_id
        """
    ).fetchall()


def _previous_cylinders(assignments: list[sqlite3.Row]) -> dict[tuple[int, int], str]:
    previous = {}
    by_item = defaultdict(list)
    for assignment in assignments:
        by_item[assignment["sharepoint_item_id"]].append(assignment)
    for item_assignments in by_item.values():
        for index, assignment in enumerate(item_assignments):
            if index:
                previous[
                    (
                        assignment["sharepoint_item_id"],
                        assignment["start_version_id"],
                    )
                ] = item_assignments[index - 1]["cylinder_number"]
    return previous


def generate_report() -> Path:
    if not DATABASE_PATH.exists():
        raise FileNotFoundError(
            "gas_bottle_history.db does not exist. Run collect_history.py first."
        )

    generated_at = datetime.now(timezone.utc)
    report_year = generated_at.year
    report_key = f"{report_year}-YTD"
    year_start = datetime(report_year, 1, 1, tzinfo=timezone.utc)
    with closing(sqlite3.connect(DATABASE_PATH, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS report_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_key TEXT UNIQUE,
                    generated_at TEXT NOT NULL,
                    period_start TEXT,
                    period_end TEXT NOT NULL,
                    output_file TEXT NOT NULL,
                    new_version_count INTEGER NOT NULL,
                    replacement_count INTEGER NOT NULL
                )
                """
            )
            report_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(report_runs)")
            }
            if "report_key" not in report_columns:
                connection.execute(
                    "ALTER TABLE report_runs ADD COLUMN report_key TEXT"
                )
            legacy_runs = connection.execute(
                """
                SELECT id, output_file
                FROM report_runs
                WHERE report_key IS NULL
                """
            ).fetchall()
            for saved_run in legacy_runs:
                match = re.search(
                    r"gas-bottle-report-(\d{4}-(?:Q[1-4]|YTD))\.html$",
                    saved_run["output_file"],
                )
                if match:
                    connection.execute(
                        "UPDATE report_runs SET report_key = ? WHERE id = ?",
                        (match.group(1), saved_run["id"]),
                    )
            connection.execute(
                """
                DELETE FROM report_runs
                WHERE report_key IS NOT NULL
                  AND id NOT IN (
                      SELECT MAX(id)
                      FROM report_runs
                      WHERE report_key IS NOT NULL
                      GROUP BY report_key
                  )
                """
            )
            connection.execute(
                "DROP INDEX IF EXISTS report_runs_by_key"
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX report_runs_by_key
                ON report_runs (report_key)
                """
            )

        assignments = _load_assignments(connection)
        previous_cylinders = _previous_cylinders(assignments)

        new_version_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM history_versions
            WHERE first_collected_at >= ?
              AND first_collected_at <= ?
            """,
            (year_start.isoformat(), generated_at.isoformat()),
        ).fetchone()[0]

        current_count = sum(row["is_current"] for row in assignments)
        valid_replacements = [
            row
            for row in assignments
            if row["is_replacement"] and row["is_valid_for_metrics"]
        ]
        yearly_counts = Counter()
        all_time_span_line_counts = Counter()
        year_to_date_replacements = []
        for row in valid_replacements:
            online = _parse_datetime(row["online_date"])
            if online:
                if online <= generated_at:
                    yearly_counts[online.year] += 1
                    all_time_span_line_counts[row["span_line"]] += 1
                if year_start <= online <= generated_at:
                    year_to_date_replacements.append(row)

        first_usage_year = min(yearly_counts, default=generated_at.year)
        usage_years = list(range(first_usage_year, generated_at.year + 1))
        usage_labels = [
            f"{year} YTD" if year == generated_at.year else str(year)
            for year in usage_years
        ]
        usage_values = [yearly_counts[year] for year in usage_years]
        usage_text = []
        for index, value in enumerate(usage_values):
            if index == 0:
                usage_text.append(f"{value}<br>(baseline)")
                continue
            previous_value = usage_values[index - 1]
            if previous_value == 0:
                usage_text.append(f"{value}<br>(N/A)")
                continue
            percentage_change = (value - previous_value) / previous_value * 100
            usage_text.append(f"{value}<br>({percentage_change:+.1f}%)")

        annual_usage = _bar_figure(
            usage_labels,
            usage_values,
            "Year",
            "Bottles used (replacements)",
        )
        annual_usage.update_traces(
            text=usage_text,
            textposition="outside",
            textfont_size=10,
            marker_color=[
                "#d4a017" if year == generated_at.year else "#17365d"
                for year in usage_years
            ],
            hovertemplate=(
                "<b>%{x}</b><br>Bottles used: %{y}<extra></extra>"
            ),
        )
        y_axis_max = (max(usage_values, default=0) // 20 + 1) * 20
        annual_usage.update_layout(
            showlegend=False,
            yaxis=dict(range=[0, y_axis_max], dtick=20),
        )

        figure_html = pio.to_html(
            annual_usage,
            full_html=False,
            include_plotlyjs="inline",
            config={"responsive": True, "displaylogo": False},
            default_width="100%",
            default_height="520px",
        )
        yearly_chart_html = f'<div class="chart">{figure_html}</div>'

        def span_line_chart(
            counts: Counter[str],
            empty_message: str,
        ) -> str:
            if not counts:
                return f'<div class="empty chart-empty">{html.escape(empty_message)}</div>'
            sorted_counts = sorted(
                counts.items(),
                key=lambda item: (item[1], item[0]),
            )[-15:]
            labels = [span_line for span_line, _ in sorted_counts]
            values = [count for _, count in sorted_counts]
            figure = _bar_figure(
                labels,
                values,
                "Bottles used (replacements)",
                "Span Line",
                horizontal=True,
            )
            figure.update_traces(
                text=values,
                textposition="outside",
                marker_color="#17365d",
                cliponaxis=False,
                hovertemplate=(
                    "<b>%{y}</b><br>Bottles used: %{x}<extra></extra>"
                ),
            )
            figure.update_layout(
                showlegend=False,
                margin=dict(l=110, r=50, t=20, b=50),
                xaxis=dict(rangemode="tozero", dtick=10),
            )
            chart_height = max(450, len(labels) * 28 + 90)
            span_figure_html = pio.to_html(
                figure,
                full_html=False,
                include_plotlyjs=False,
                config={"responsive": True, "displaylogo": False},
                default_width="100%",
                default_height=f"{chart_height}px",
            )
            return f'<div class="chart">{span_figure_html}</div>'

        all_time_span_line_chart_html = span_line_chart(
            all_time_span_line_counts,
            "No all-time replacement usage is available.",
        )

        replacement_rows = []
        for row in sorted(
            year_to_date_replacements,
            key=lambda value: (
                value["online_date"],
                value["span_line"],
            ),
            reverse=True,
        ):
            old_cylinder = previous_cylinders.get(
                (row["sharepoint_item_id"], row["start_version_id"]),
                "",
            )
            replacement_rows.append(
                [
                    row["span_line"],
                    old_cylinder,
                    row["cylinder_number"],
                    _gas_label(row),
                    _format_date(row["online_date"]),
                ]
            )

        current_rows = sorted(
            (
                [
                    row["span_line"],
                    row["cylinder_number"],
                    _gas_label(row),
                    _format_date(row["online_date"]),
                    row["days_online"],
                ]
                for row in assignments
                if row["is_current"]
            ),
            key=lambda value: value[0],
        )

        period_start = year_start.isoformat()
        REPORTS_PATH.mkdir(exist_ok=True)
        output_path = REPORTS_PATH / f"gas-bottle-report-{report_key}.html"

        report_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gas Bottle YTD Report {report_year}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; background: #f4f6f8; color: #1f2933; }}
    header {{ background: #17365d; color: white; padding: 24px 5%; }}
    main {{ max-width: 1400px; margin: auto; padding: 24px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 16px; }}
    .card, section {{ background: white; border-radius: 8px; padding: 18px; margin-bottom: 20px; box-shadow: 0 1px 4px #0002; }}
    .value {{ font-size: 2rem; font-weight: bold; color: #17365d; }}
    .charts {{ width: 100%; }}
    .chart {{ min-width: 0; width: 100%; overflow: hidden; }}
    .chart .plotly-graph-div {{ width: 100% !important; }}
    .chart-heading {{ margin: 28px 0 8px; }}
    .chart-empty {{ padding: 36px 0; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #d9e2ec; text-align: left; white-space: nowrap; }}
    th {{ background: #eaf0f6; }}
    .empty {{ text-align: center; color: #68737d; }}
    .period {{ color: #d9e2ec; }}
  </style>
</head>
<body>
<header>
  <h1>Gas Bottle Data — {report_year}</h1>
</header>
<main>
  <div class="cards">
  </div>
  <section>
    <h2>Yearly Gas Bottle Usage</h2>
    <div class="charts">
      {yearly_chart_html}
      <h3 class="chart-heading">Top 15 All-Time Bottle Usage by Span Line</h3>
      {all_time_span_line_chart_html}
    </div>
  </section>
  <section>
    <h2>Replacements Year to Date</h2>
    {_table(
        ["Span Line", "Old Cylinder", "New Cylinder", "Gas", "Online Date"],
        replacement_rows,
    )}
  </section>
  <section>
    <h2>Current Cylinders</h2>
    {_table(
        ["Span Line", "Cylinder", "Gas", "Online Date", "Days Online"],
        current_rows,
    )}
  </section>
</main>
</body>
</html>
"""
        output_path.write_text(report_html, encoding="utf-8")

        with connection:
            connection.execute(
                """
                INSERT INTO report_runs (
                    report_key,
                    generated_at,
                    period_start,
                    period_end,
                    output_file,
                    new_version_count,
                    replacement_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (report_key) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    period_start = excluded.period_start,
                    period_end = excluded.period_end,
                    output_file = excluded.output_file,
                    new_version_count = excluded.new_version_count,
                    replacement_count = excluded.replacement_count
                """,
                (
                    report_key,
                    generated_at.isoformat(),
                    period_start,
                    generated_at.isoformat(),
                    str(output_path),
                    new_version_count,
                    len(year_to_date_replacements),
                ),
            )

    return output_path


def main() -> None:
    output_path = generate_report()
    print(f"Report: {output_path}")


if __name__ == "__main__":
    main()
