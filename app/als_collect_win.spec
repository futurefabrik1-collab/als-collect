# -*- mode: python ; coding: utf-8 -*-
# EXPERIMENTAL Windows build of ALS Collect (onedir: "ALS Collect\ALS Collect.exe").
# Built by .github/workflows/build.yml on windows-latest; untested on a real PC.
import os
here = os.path.dirname(os.path.abspath(SPEC))
root = os.path.dirname(here)

a = Analysis(
    [os.path.join(here, 'als_app.py')],
    pathex=[root, here],
    datas=[(os.path.join(here, 'ui'), 'ui')],
    hiddenimports=['als_collect', 'als_preview', 'media_server', 'midi_render',
                   'webview.platforms.edgechromium', 'webview.platforms.winforms', 'clr'],
    excludes=['tkinter', 'unittest', 'test', 'pydoc', 'xmlrpc'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ALS Collect', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ALS Collect')
