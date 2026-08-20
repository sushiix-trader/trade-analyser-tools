"""PyInstaller one-file build for the Windows desktop GUI.

Build from the repository root with:

    python -m PyInstaller --clean --noconfirm packaging/windows/trade-analyser-gui.spec

The spec intentionally starts at ``gui.launcher`` and bundles the existing
package data plus Matplotlib's dynamically loaded modules. Analytical work
still runs through the public ``analyser`` API at runtime.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).parents[1]

analysis = Analysis(
    [str(project_root / "gui" / "launcher.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=collect_data_files("analyser") + collect_data_files("matplotlib"),
    hiddenimports=collect_submodules("matplotlib"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="trade-analyser-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
