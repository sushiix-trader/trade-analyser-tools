"""Tkinter desktop application for one MT5 report analysis.

The window is intentionally a thin adapter.  All report work is delegated to
``gui.workflow.run_analysis`` which, in turn, calls only the public
``analyser`` API.  Keeping Tkinter here makes it possible to replace the UI
without duplicating the platform's analytical behaviour.
"""

from __future__ import annotations

from pathlib import Path
import threading
import webbrowser
from typing import Any

from analyser import MonteCarloConfig

from .workflow import GuiRunConfig, GuiRunResult, run_analysis


class ReportAnalyzerApp:
    """A small desktop front end for single-report analysis."""

    _BACKGROUND = "#0b1f3a"
    _PANEL = "#102b4e"
    _TEXT = "#f4f7fb"
    _MUTED = "#b9c8da"
    _ACCENT = "#42b6ff"
    _SUCCESS = "#8ee6b2"
    _ERROR = "#ff9c9c"

    def __init__(self, root: Any | None = None) -> None:
        self._tk, self._ttk, self._filedialog, self._messagebox = _load_tkinter()
        self.root = root or self._tk.Tk()
        self._last_run: GuiRunResult | None = None
        self._busy = False

        self.source_var = self._tk.StringVar()
        self.output_var = self._tk.StringVar()
        self.monte_carlo_var = self._tk.BooleanVar(value=False)
        self.method_var = self._tk.StringVar(value="permutation")
        self.iterations_var = self._tk.StringVar(value="1000")
        self.seed_var = self._tk.StringVar(value="42")
        self.path_chart_var = self._tk.BooleanVar(value=True)
        self.status_var = self._tk.StringVar(value="Select one MT5 HTML or XML report to begin.")

        self._configure_window()
        self._build_widgets()
        self._update_monte_carlo_state()

    def run(self) -> None:
        """Enter the Tkinter event loop."""

        self.root.mainloop()

    def _configure_window(self) -> None:
        self.root.title("MT5 Strategy Report Analyzer")
        self.root.geometry("900x680")
        self.root.minsize(760, 560)
        self.root.configure(background=self._BACKGROUND)
        style = self._ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except self._tk.TclError:
            pass
        style.configure("App.TFrame", background=self._BACKGROUND)
        style.configure("Panel.TFrame", background=self._PANEL)
        style.configure("Title.TLabel", background=self._BACKGROUND, foreground=self._TEXT, font=("TkDefaultFont", 18, "bold"))
        style.configure("Subtitle.TLabel", background=self._BACKGROUND, foreground=self._MUTED)
        style.configure("Panel.TLabel", background=self._PANEL, foreground=self._TEXT)
        style.configure("Muted.Panel.TLabel", background=self._PANEL, foreground=self._MUTED)
        style.configure("Accent.TButton", foreground=self._BACKGROUND)
        style.configure("Status.TLabel", background=self._BACKGROUND, foreground=self._MUTED)
        style.configure("Warning.TLabel", background=self._PANEL, foreground="#ffd18a")

    def _build_widgets(self) -> None:
        root_frame = self._ttk.Frame(self.root, style="App.TFrame", padding=24)
        root_frame.pack(fill="both", expand=True)
        root_frame.columnconfigure(0, weight=1)
        root_frame.rowconfigure(4, weight=1)

        self._ttk.Label(
            root_frame,
            text="MT5 Strategy Report Analyzer",
            style="Title.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self._ttk.Label(
            root_frame,
            text="Generate the canonical interactive report, then optionally run deterministic Monte Carlo simulations.",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 18))

        input_panel = self._ttk.Frame(root_frame, style="Panel.TFrame", padding=16)
        input_panel.grid(row=2, column=0, sticky="ew")
        input_panel.columnconfigure(1, weight=1)
        self._ttk.Label(input_panel, text="Report", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=5)
        self._ttk.Entry(input_panel, textvariable=self.source_var).grid(row=0, column=1, sticky="ew", pady=5)
        self._ttk.Button(input_panel, text="Choose report…", command=self._choose_source).grid(row=0, column=2, padx=(12, 0), pady=5)
        self._ttk.Label(input_panel, text="Output folder", style="Panel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=5)
        self._ttk.Entry(input_panel, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", pady=5)
        self._ttk.Button(input_panel, text="Choose folder…", command=self._choose_output).grid(row=1, column=2, padx=(12, 0), pady=5)
        self._ttk.Label(
            input_panel,
            text="Accepted inputs: one completed-position MT5 Strategy Tester report (.htm, .html, or .xml).",
            style="Muted.Panel.TLabel",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        mc_panel = self._ttk.Frame(root_frame, style="Panel.TFrame", padding=16)
        mc_panel.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        mc_panel.columnconfigure(4, weight=1)
        mc_check = self._ttk.Checkbutton(
            mc_panel,
            text="Run Monte Carlo",
            variable=self.monte_carlo_var,
            command=self._update_monte_carlo_state,
        )
        mc_check.grid(row=0, column=0, sticky="w", padx=(0, 20))
        self._ttk.Label(mc_panel, text="Method", style="Panel.TLabel").grid(row=0, column=1, sticky="w")
        self.method_combo = self._ttk.Combobox(
            mc_panel,
            textvariable=self.method_var,
            values=("permutation", "bootstrap"),
            state="readonly",
            width=14,
        )
        self.method_combo.grid(row=0, column=2, sticky="w", padx=(8, 20))
        self._ttk.Label(mc_panel, text="Iterations", style="Panel.TLabel").grid(row=0, column=3, sticky="w")
        self.iterations_entry = self._ttk.Entry(mc_panel, textvariable=self.iterations_var, width=10)
        self.iterations_entry.grid(row=0, column=4, sticky="w", padx=(8, 20))
        self._ttk.Label(mc_panel, text="Seed", style="Panel.TLabel").grid(row=0, column=5, sticky="w")
        self.seed_entry = self._ttk.Entry(mc_panel, textvariable=self.seed_var, width=10)
        self.seed_entry.grid(row=0, column=6, sticky="w", padx=(8, 0))
        self.path_chart_check = self._ttk.Checkbutton(
            mc_panel,
            text="Generate path chart with 5–95% / 25–75% bands and streak panels",
            variable=self.path_chart_var,
        )
        self.path_chart_check.grid(row=1, column=0, columnspan=7, sticky="w", pady=(12, 0))
        self._ttk.Label(
            mc_panel,
            text="Permutation preserves the observed trade outcomes and changes their order. Bootstrap samples with replacement.",
            style="Muted.Panel.TLabel",
        ).grid(row=2, column=0, columnspan=7, sticky="w", pady=(8, 0))

        action_row = self._ttk.Frame(root_frame, style="App.TFrame")
        action_row.grid(row=4, column=0, sticky="nsew", pady=(14, 0))
        action_row.columnconfigure(0, weight=1)
        action_row.rowconfigure(1, weight=1)
        self.generate_button = self._ttk.Button(
            action_row,
            text="Generate report",
            command=self._start_run,
        )
        self.generate_button.grid(row=0, column=0, sticky="w")
        self.status_label = self._ttk.Label(action_row, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.grid(row=0, column=0, sticky="e")

        output_frame = self._ttk.Frame(action_row, style="Panel.TFrame", padding=12)
        output_frame.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        self.output_text = self._tk.Text(
            output_frame,
            height=12,
            wrap="word",
            background="#08182e",
            foreground=self._TEXT,
            insertbackground=self._TEXT,
            relief="flat",
            padx=10,
            pady=10,
        )
        self.output_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = self._ttk.Scrollbar(output_frame, orient="vertical", command=self.output_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.output_text.configure(yscrollcommand=scrollbar.set, state="disabled")

        button_row = self._ttk.Frame(output_frame, style="Panel.TFrame")
        button_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.open_report_button = self._ttk.Button(button_row, text="Open HTML report", command=self._open_report, state="disabled")
        self.open_report_button.pack(side="left")
        self.open_folder_button = self._ttk.Button(button_row, text="Open output folder", command=self._open_folder, state="disabled")
        self.open_folder_button.pack(side="left", padx=(8, 0))

    def _choose_source(self) -> None:
        path = self._filedialog.askopenfilename(
            title="Choose an MT5 Strategy Tester report",
            filetypes=[
                ("MT5 reports", "*.htm *.html *.xml"),
                ("HTML reports", "*.htm *.html"),
                ("XML reports", "*.xml"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        selected = Path(path)
        self.source_var.set(str(selected))
        if not self.output_var.get().strip():
            self.output_var.set(str(selected.parent / "analysis-output"))

    def _choose_output(self) -> None:
        path = self._filedialog.askdirectory(title="Choose an output folder")
        if path:
            self.output_var.set(path)

    def _update_monte_carlo_state(self) -> None:
        state = "normal" if self.monte_carlo_var.get() and not self._busy else "disabled"
        combo_state = "readonly" if state == "normal" else "disabled"
        self.method_combo.configure(state=combo_state)
        for widget in (self.iterations_entry, self.seed_entry):
            widget.configure(state=state)
        self.path_chart_check.configure(state=state)

    def _start_run(self) -> None:
        if self._busy:
            return
        source_text = self.source_var.get().strip()
        output_text = self.output_var.get().strip()
        if not source_text:
            self._show_error("Choose an MT5 HTML or XML report first.")
            return
        if not output_text:
            self._show_error("Choose an output folder first.")
            return

        monte_carlo: MonteCarloConfig | None = None
        if self.monte_carlo_var.get():
            try:
                iterations = int(self.iterations_var.get().strip())
                seed = int(self.seed_var.get().strip())
                if iterations < 1:
                    raise ValueError("iterations must be greater than zero")
                monte_carlo = MonteCarloConfig(
                    iterations=iterations,
                    method=self.method_var.get(),
                    seed=seed,
                )
            except ValueError as exc:
                self._show_error(f"Monte Carlo settings are invalid: {exc}")
                return

        config = GuiRunConfig(
            source=source_text,
            output_dir=output_text,
            monte_carlo=monte_carlo,
            generate_monte_carlo_chart=self.path_chart_var.get(),
        )
        self._set_busy(True)
        self._set_output("Working… the report is being parsed and analysed eagerly.\n")
        thread = threading.Thread(target=self._worker, args=(config,), daemon=True)
        thread.start()

    def _worker(self, config: GuiRunConfig) -> None:
        try:
            result = run_analysis(config)
        except Exception as exc:  # surfaced on the UI thread with context
            self.root.after(0, self._run_failed, exc)
            return
        self.root.after(0, self._run_completed, result)

    def _run_completed(self, result: GuiRunResult) -> None:
        self._last_run = result
        self._set_busy(False)
        lines = ["Generated artifacts:", *[f"• {path}" for path in result.output_paths]]
        if result.warnings:
            lines.extend(["", "Warnings:", *[f"• {warning}" for warning in result.warnings]])
        self._set_output("\n".join(lines) + "\n")
        self.status_var.set("Complete. The interactive HTML report is ready.")
        self.open_report_button.configure(state="normal")
        self.open_folder_button.configure(state="normal")
        self._open_report()

    def _run_failed(self, error: Exception) -> None:
        self._set_busy(False)
        self.status_var.set("The analysis did not complete.")
        self._show_error(str(error))
        self._set_output(f"Error: {error}\n")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.generate_button.configure(state="disabled" if busy else "normal")
        self._update_monte_carlo_state()

    def _set_output(self, text: str) -> None:
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", text)
        self.output_text.configure(state="disabled")

    def _open_report(self) -> None:
        if self._last_run is not None:
            webbrowser.open(self._last_run.report_path.resolve().as_uri())

    def _open_folder(self) -> None:
        if self._last_run is not None:
            _open_path(self._last_run.report_path.parent)

    def _show_error(self, message: str) -> None:
        self._messagebox.showerror("MT5 Strategy Report Analyzer", message)


def _load_tkinter() -> tuple[Any, Any, Any, Any]:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:  # pragma: no cover - depends on system Tk install
        raise RuntimeError(
            "Tkinter is required for the desktop GUI. On Debian/Ubuntu install python3-tk."
        ) from exc
    return tk, ttk, filedialog, messagebox


def _open_path(path: Path) -> None:
    """Open a folder with the host's default file browser."""

    webbrowser.open(path.resolve().as_uri())


def main() -> None:
    """Launch the desktop application."""

    app = ReportAnalyzerApp()
    app.run()


if __name__ == "__main__":  # pragma: no cover - exercised by a desktop user
    main()
