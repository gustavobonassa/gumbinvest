# Auto-import folder

Anything you drop here is imported automatically on the next startup (and
re-running is always safe — files already imported add nothing).

**Filenames don't matter.** The importer detects each file's type from its
contents, and the whole folder is scanned recursively. The subfolders are just
a convention to keep your archive organised:

| Folder | What to put in it |
|---|---|
| `b3/` | The "Movimentação" CSV(s) exported from B3's investor area |
| `avenue/` | Avenue monthly statement PDFs |
| `nomad/` | Nomad monthly statement PDFs |
| `binance/` | Binance CSV exports (transaction / trade / order history) |

Files at the root of `data/` work too.

Everything in here is **gitignored** except this README and the folder
placeholders — your real financial history never reaches the repository.
