# -*- mode: python ; coding: utf-8 -*-
# Build:  .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm MazCro.spec
# Output: dist\MazCro.exe  (single self-contained file, no extra folders needed)
"""PyInstaller build configuration for MazCro.

Uses onefile mode so users can download and run a single MazCro.exe without
needing a Python installation or an accompanying _internal directory.
"""

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # pynput picks its backend at runtime via OS checks, so the win32
        # implementations are invisible to static analysis and must be named.
        'pynput._util.win32',
        'pynput._util.win32_vks',
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
        # pyautogui likewise imports its platform backend indirectly.
        'pyautogui._pyautogui_win',
        'mouseinfo',
        'pygetwindow',
        'pyperclip',
        'pymsgbox',
        'pytweening',
        'pyrect',
        'pyscreeze',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
        'pytest',
        'notebook',
        'IPython',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='MazCro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
