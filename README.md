# Gas Bottle History and Reporting

This project archives the SharePoint version history for every Span Line in the
**Online Gas Bottles** view and produces a standalone interactive year-to-date
report.

## Setup

Requirements:

- Windows signed in with an AVL account
- AVL network or VPN access
- Python 3.11 or newer

Install the Python packages:

```powershell
python -m pip install -r requirements.txt
```

## Weekly collection

Run:

```powershell
python collect_history.py
```

The collector:

- Authenticates with the signed-in Windows user.
- Reads all SharePoint versions for the current Online Gas Bottles items.
- Adds only previously unseen item/version pairs to `gas_bottle_history.db`.
- Never deletes the canonical history archive.
- Rebuilds cylinder assignments used by the report.
- Records each attempt in `collection_runs`.

The canonical `history_versions` table is the source for all historical
analysis. Filter it by `span_line` to inspect one line. Retired Span Lines remain
archived; use `tracked_items.is_active` to distinguish current and retired
items.

## Year-to-date report

Run:

```powershell
python generate_report.py
```

The report is written to:

```text
reports\gas-bottle-report-YYYY-YTD.html
```

It is a self-contained HTML file with Plotly embedded. It can be opened in a
browser or forwarded without a web server or internet connection.

Each report includes:

- Bottle usage totals and year-over-year percentage changes for all archived years.
- All-time bottle usage for the 15 highest-usage Span Lines.
- Replacement events from January 1 through the report date.
- Current cylinders and their days online.

The report uses cylinder replacements as the bottle-usage indicator.
Physical gas volume requires additional cylinder capacity, starting
pressure/quantity, and residual pressure/quantity data.

## Windows Task Scheduler

Create two tasks while signed in as the AVL user who can open the SharePoint
page.

### Weekly collector

1. Open **Task Scheduler** and select **Create Task**.
2. Name it `Gas Bottle Weekly Collection`.
3. Select **Run only when user is logged on** so Windows integrated
   authentication is available.
4. Add a weekly trigger for a time when the PC is normally on and connected to
   the AVL network or VPN.
5. Add an action:
   - **Program/script:** the full path to `python.exe`
   - **Add arguments:** the full quoted path to `collect_history.py`
   - **Start in:** this project directory
6. Enable **Run task as soon as possible after a scheduled start is missed**.
7. Set a reasonable stop limit, such as one hour.
8. On laptops, clear **Start the task only if the computer is on AC power** if
   collection should also run on battery.
9. Run the task manually once and confirm a successful row appears in
   `collection_runs`.

Example action:

```text
Program/script: C:\Path\To\python.exe
Add arguments:  "C:\Path\To\Gas-Tracker\collect_history.py"
Start in:       C:\Path\To\Gas-Tracker
```

### Year-to-date report

Create a second task named `Gas Bottle YTD Report`:

1. Add a **Monthly** trigger and select January, April, July, and October.
2. Run it after the weekly collector.
3. Use `generate_report.py` as the script argument.
4. Confirm the current year's YTD report appears under `reports\`.

Each run updates the same YTD report for the current calendar year.

## Recommended routine

1. Let collection run weekly.
2. Check failed collection runs before generating the report:

   ```sql
   SELECT *
   FROM collection_runs
   WHERE status = 'failed'
   ORDER BY id DESC;
   ```

3. Generate the YTD report on the desired schedule.
4. Forward the generated HTML file or place it in an approved shared location.

## Important database tables

| Table | Purpose |
|---|---|
| `history_versions` | Append-only canonical SharePoint history |
| `cylinder_assignments` | Derived cylinder service periods and quality flags |
| `collection_runs` | Weekly collection audit log |
| `report_runs` | Year-to-date report periods and outputs |

The database and generated reports are intentionally excluded from Git because
they contain internal operational data.
