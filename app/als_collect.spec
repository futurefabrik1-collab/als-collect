# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for "ALS Collect.app"  (build with app/build_app.sh)
import os
here = os.path.dirname(os.path.abspath(SPEC))
root = os.path.dirname(here)

a = Analysis(
    [os.path.join(here, 'als_app.py')],
    pathex=[root, here],
    datas=[(os.path.join(here, 'ui'), 'ui')],
    hiddenimports=['als_collect', 'als_preview', 'media_server', 'midi_render', 'AVFoundation',
                   'webview.platforms.cocoa', 'objc', 'AppKit', 'Foundation', 'WebKit',
                   'Quartz', 'Security', 'UniformTypeIdentifiers'],
    excludes=['tkinter', 'unittest', 'test', 'pydoc', 'xmlrpc'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ALS Collect', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          argv_emulation=False, target_arch=None, codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ALS Collect')
app = BUNDLE(
    coll, name='ALS Collect.app', icon=None, bundle_identifier='com.futurefabrik.alscollect',
    info_plist={
        'CFBundleName': 'ALS Collect', 'CFBundleDisplayName': 'ALS Collect',
        'CFBundleShortVersionString': '0.2.0', 'CFBundleVersion': '2',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'LSMinimumSystemVersion': '12.0',
        # lets Finder offer "Open with ALS Collect" on .als files; Live stays the default
        'CFBundleDocumentTypes': [{
            'CFBundleTypeName': 'Ableton Live Set', 'CFBundleTypeRole': 'Viewer',
            'LSHandlerRank': 'Alternate',
            'LSItemContentTypes': ['com.ableton.live-set'],
            'CFBundleTypeExtensions': ['als'],
        }],
        'UTImportedTypeDeclarations': [{
            'UTTypeIdentifier': 'com.ableton.live-set', 'UTTypeDescription': 'Ableton Live Set',
            'UTTypeConformsTo': ['public.data'],
            'UTTypeTagSpecification': {'public.filename-extension': ['als']},
        }],
    },
)
