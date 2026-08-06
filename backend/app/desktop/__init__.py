"""Desktop (no-Docker) launcher: tray icon, SQLite, in-process scheduler.

Nothing in ``app.main`` imports this package — Docker deployments never load
it. The reverse import direction (desktop → app) is the normal one.
"""
