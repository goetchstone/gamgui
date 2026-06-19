# PyInstaller spec — builds GamGUI.app (macOS).
# Use scripts/build_app.sh, which vendors GAM and installs PyInstaller + pywebview first.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("gamgui/web/templates", "gamgui/web/templates"),
    ("gamgui/web/static", "gamgui/web/static"),
]
binaries = []
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("keyring.backends")
    + ["uvicorn.lifespan.on", "uvicorn.loops.auto", "uvicorn.protocols.http.auto"]
)

# pywebview + its macOS (Cocoa/WebKit via pyobjc) backend — collect everything it needs.
_wv_datas, _wv_binaries, _wv_hidden = collect_all("webview")
datas += _wv_datas
binaries += _wv_binaries
hiddenimports += _wv_hidden

# Bundle the vendored GAM7 binary (resolved at runtime via sys._MEIPASS/resources/gam7/gam).
if os.path.isdir("gamgui/resources/gam7") and os.path.exists("gamgui/resources/gam7/gam"):
    datas.append(("gamgui/resources/gam7", "resources/gam7"))

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="GamGUI", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="GamGUI")
app = BUNDLE(
    coll,
    name="GamGUI.app",
    icon=None,
    bundle_identifier="com.saybrookhome.gamgui",
    info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0"},
)
