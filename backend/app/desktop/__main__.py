"""``python -m app.desktop`` — run the desktop server without packaging.

Pair it with the Electron shell in dev (`npm start` in desktop-shell/), or
just open http://localhost:8873 in a browser.
"""
from app.desktop.headless import main

main()
