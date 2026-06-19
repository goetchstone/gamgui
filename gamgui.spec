# PyInstaller spec — builds GamGUI.app (macOS).
# Use scripts/build_app.sh, which vendors GAM and installs PyInstaller first.
import os

from PyInstaller.utils.hooks import collect_submodules

datas = [
    ("gamgui/web/templates", "gamgui/web/templates"),
    ("gamgui/web/static", "gamgui/web/static"),
]
# Bundle the vendored GAM7 binary (resolved at runtime via sys._MEIPASS/resources/gam7/gam).
if os.path.isdir("gamgui/resources/gam7") and os.path.exists("gamgui/resources/gam7/gam"):
    datas.append(("gamgui/resources/gam7", "resources/gam7"))

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("keyring.backends")
    + ["uvicorn.lifespan.on", "uvicorn.loops.auto", "uvicorn.protocols.http.auto"]
)

a = Analysis(
    ["gamgui/app.py"],
    pathex=[],
    binaries=[],
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
