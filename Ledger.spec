# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Ledger.

Build with:

    pyinstaller Ledger.spec --noconfirm

Everything that PyInstaller cannot work out by itself is declared here rather
than passed on the command line, so a build is one reproducible command and the
reasoning stays next to the settings.

Produces dist/Ledger/ — a folder, not a single file. That is deliberate; see
the note on onedir at the bottom.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


# ---------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------

datas = [
    # The web UI. It is not a .py file, so nothing includes it automatically,
    # and without it the server runs but serves a 500 for the UI.
    ("ledger/static", "ledger/static"),
]

# The CA bundle used to verify HTTPS. Without it every call to Google fails
# certificate verification — the app starts fine and then cannot transcribe
# anything, which is a slow way to discover a packaging problem.
datas += collect_data_files("certifi")

# The IANA timezone database.
#
# All quota accounting keys off the US Pacific date, because that is when Google
# resets daily limits, and zoneinfo resolves that name against the operating
# system's timezone database — which Windows does not have. quota.py builds its
# ZoneInfo at module level, so a missing database is an IMPORT-time crash that
# takes the whole application down rather than degrading.
datas += collect_data_files("tzdata")


# ---------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------

hiddenimports = [
    # uvicorn selects its event loop and protocol implementations by importing
    # them from strings at runtime, so static analysis never sees them.
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "h11",

    # zoneinfo needs the package itself present, not only its data.
    "tzdata",
]

# Pydantic v2 builds validators dynamically and its core is a compiled Rust
# extension; collecting the submodules avoids a scatter of missing-module
# errors at first request rather than at startup.
hiddenimports += collect_submodules("pydantic")


# ---------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------

a = Analysis(
    ["ledger_app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here is needed at runtime, and each pulls in a lot of weight.
    excludes=[
        "tkinter",
        "matplotlib",
        "pytest",
        "IPython",
        "notebook",
        "PIL.ImageQt",
        "PIL.ImageTk",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Ledger",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX compression is a common antivirus false-positive trigger
    console=True,       # the window carries the URL and the startup errors
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="ledger.ico",  # uncomment once you have an icon
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Ledger",
)

# ---------------------------------------------------------------------
# Why onedir rather than onefile
# ---------------------------------------------------------------------
#
# A onefile build unpacks the entire bundle to a temporary directory on every
# launch. With PyMuPDF, numpy and pydantic-core that is a few hundred megabytes
# of extraction and several seconds of startup each time, and it is a frequent
# cause of antivirus interference because an executable writing and then
# executing files in temp looks exactly like malware behaviour.
#
# For something that runs for weeks at a stretch, onedir starts instantly and
# stays out of the way. Wrap dist/Ledger in an installer (Inno Setup) if you
# want a single file to hand around.
