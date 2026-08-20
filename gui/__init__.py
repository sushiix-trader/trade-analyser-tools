"""Optional desktop front end for the public :mod:`analyser` API.

Use ``python -m gui`` to launch the Tkinter application.  The framework-free
workflow is available from :mod:`gui.workflow` for tests and future front ends.
"""

from .workflow import GuiRunConfig, GuiRunResult, run_analysis

__all__ = ["GuiRunConfig", "GuiRunResult", "run_analysis"]
