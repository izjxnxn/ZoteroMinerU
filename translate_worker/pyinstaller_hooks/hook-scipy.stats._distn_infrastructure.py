# Run from source: executed from PyInstaller's archive, this module's clean-up loop
# `for obj in [s for s in dir() if s.startswith('_doc_')]` sees no names and `del obj` fails (NameError at import).
module_collection_mode = "py"
