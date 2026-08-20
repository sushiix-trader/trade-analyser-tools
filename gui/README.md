# Desktop GUI

The `gui/` folder contains the desktop front end for the trade analyser. It is
an intentionally thin Tkinter adapter over the public `analyser` package. The
GUI does **not** parse MT5 markup, calculate ratios, reconstruct curves, or
implement Monte Carlo sampling itself.

That separation is important:

```text
Tkinter window (gui/app.py)
        |
        v
GUI workflow (gui/workflow.py)
        |
        v
Public analyser API (analyze_file, save_interactive_report, run_monte_carlo, ...)
```

If the GUI needs a capability that is not available in `analyser`, add that
capability to the public typed analyser API first. Do not add a one-off parser,
metric calculation, simulation, or chart implementation to the GUI.

## Install

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[gui]"
```

The GUI uses Python's standard-library Tkinter. On Debian or Ubuntu, install
Tkinter at the operating-system level if it is not already present:

```bash
sudo apt-get install python3-tk
```

The `gui` extra installs Matplotlib for the optional standalone equity/drawdown
and Monte Carlo path PNGs. The self-contained interactive HTML report and JSON/
Markdown outputs are generated through the analyser API regardless; if
Matplotlib is unavailable, the GUI reports that the standalone PNG was skipped.

## Launch

Either launch the module directly:

```bash
.venv/bin/python -m gui
```

or, after installing the project, use the console command:

```bash
trade-analyser-gui
```

## Workflow

1. Click **Choose report** and select one completed-position MetaTrader 5
   Strategy Tester report (`.htm`, `.html`, or `.xml`).
2. Choose the output folder. The default suggested by the GUI is an
   `analysis-output` folder beside the selected report.
3. Leave **Run Monte Carlo** unchecked for the eager report only, or enable it
   to expose the deterministic simulation controls.
4. Choose `permutation` or `bootstrap`, set the iteration count and seed, and
   optionally generate the path chart.
5. Click **Generate report**. The GUI performs the work off the Tkinter event
   loop, opens the generated HTML report in the default browser, and lists all
   output paths in the window.

The browser report is a self-contained HTML file. It can be opened and shared
without running Python or starting a server.

## Monte Carlo controls

The GUI exposes the conservative, reproducible controls already provided by
the public API:

- **Permutation** preserves the observed completed-position net-profit outcomes
  and changes only their order.
- **Bootstrap** samples the observed outcomes with replacement.
- **Iterations** controls the number of simulated paths.
- **Seed** fixes the random generator state so the result can be recreated.
- **Path chart** retains a bounded, evenly spaced subset of paths (up to 500)
  and uses the public chart API to draw 5–95% and 25–75% bands, drawdown, and
  winning/losing streak panels.

The simulation is run from the already parsed canonical report held by the
`AnalysisResult`; the GUI does not read or parse the source a second time.
Monte Carlo remains a one-strategy feature in this first GUI slice. Portfolio
Monte Carlo, skipped-trade stress controls, and ruin-threshold controls remain
available through the analyser API but are not exposed as GUI inputs yet.

## Generated outputs

For a source named `my-strategy.html`, the GUI writes deterministic names such
as:

| File | Purpose |
|---|---|
| `my-strategy-interactive-report.html` | Preferred self-contained interactive analysis report |
| `my-strategy-analysis.json` | Full typed `AnalysisResult` serializer |
| `my-strategy-analysis.md` | Human-readable analysis serializer |
| `my-strategy-equity-drawdown.png` | Optional standalone equity/drawdown chart |
| `my-strategy-monte-carlo-summary.json` | Percentile summaries, streak summaries, and ruin probability |
| `my-strategy-monte-carlo.json` | Full deterministic Monte Carlo serializer |
| `my-strategy-monte-carlo-paths.png` | Optional simulated paths, intervals, drawdown, and streak panels |

Monte Carlo files are created only when the option is enabled. PNG files are
created only when the optional chart dependency is available.

The interactive HTML report contains the platform's existing report views,
including metrics, monthly performance, monthly drawdown, equity/drawdown,
trade analysis, and portfolio correlation when a portfolio result is supplied
through the analyser API.

## Framework-free workflow seam

The Tkinter layer is not required to use the orchestration. The framework-free
workflow can be called by another UI or a future packaged desktop shell:

```python
from analyser import MonteCarloConfig
from gui.workflow import GuiRunConfig, run_analysis

run = run_analysis(
    GuiRunConfig(
        source="reports/my-strategy.html",
        output_dir="analysis-output",
        monte_carlo=MonteCarloConfig(
            iterations=1_000,
            method="permutation",
            seed=42,
        ),
        generate_monte_carlo_chart=True,
    )
)

print(run.report_path)
print(run.analysis_result.metrics.net_profit)
print(run.monte_carlo_result.summary() if run.monte_carlo_result else "Monte Carlo disabled")
```

This is an orchestration example, not a replacement for the public analyser
API. The analytical values still come from `AnalysisResult` and
`MonteCarloResult`.

## Scope and limitations

- The GUI currently accepts one single-run MT5 report at a time.
- Optimization workbooks, unsupported account-history exports, and unhydrated
  Git LFS pointers are rejected by the analyser API.
- The GUI does not currently expose portfolios, member weights, filters,
  in-sample/out-of-sample configuration, or what-if sizing controls. Those
  remain typed analyser capabilities and can be added as GUI controls later
  without moving analytical logic into the front end.
- Monte Carlo operates on completed-position net profits. It does not recreate
  intrabar floating equity or supply live execution decisions.
- This is an analysis tool only. It has no broker connection, order placement,
  live execution, or trading automation.

## Verification

Run the GUI workflow tests and the full repository suite from the repository
root:

```bash
python3 -m unittest discover -s tests -v
ruff check analyser gui tests
```

The GUI workflow tests run headlessly. They verify that the HTML, serializers,
and Monte Carlo artifacts are produced through the public analyser seams; they
do not require a display server.

## Windows executable for end users

The repository includes a PyInstaller specification at
`packaging/windows/trade-analyser-gui.spec`. It creates a portable, one-file
Windows executable. End users do not need Python, Tkinter, Matplotlib, or this
repository installed when using the built `.exe`.

### Build locally on Windows

A Windows build must be performed on Windows. From PowerShell at the repository
root:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[gui,packaging]"
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm packaging/windows/trade-analyser-gui.spec
```

The executable will be written to:

```text
dist\trade-analyser-gui.exe
```

Run it by double-clicking the file or from PowerShell:

```powershell
.\dist\trade-analyser-gui.exe
```

The build uses `console=False`, so end users get a normal desktop window rather
than an additional console window. Generated reports are written to the output
folder selected inside the application.

### Build through GitHub Actions

The repository also contains
`.github/workflows/build-windows-executable.yml`. It runs on `windows-latest`
and uploads `trade-analyser-gui.exe` as a workflow artifact.

To build it manually:

1. Open the repository on GitHub.
2. Open **Actions**.
3. Select **Build Windows GUI executable**.
4. Click **Run workflow** and choose the `feature/desktop-gui` branch or the
   branch containing the packaging changes.
5. Download the `trade-analyser-gui-windows` artifact from the completed run.

The workflow also runs automatically when a version tag such as `v1.1.0` is
pushed. The executable is currently an unsigned build; Windows SmartScreen may
show a warning until the application is distributed with a code-signing
certificate.

### Distribution notes

- Build the executable on Windows; do not try to copy a Linux executable and
  rename it to `.exe`.
- Distribute the generated `.exe` over HTTPS or as a GitHub Actions/Release
  artifact, and publish a SHA-256 checksum for users who want to verify it.
- The application does not connect to MetaTrader, place trades, or upload
  reports. It processes the selected report locally and writes local outputs.
- The executable does not include private reports or repository sample data;
  those are selected by the user at runtime.
