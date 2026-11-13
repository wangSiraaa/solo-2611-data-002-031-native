# Erasure and hold inputs
Run `python3 fixtures/prepare.py work-input` to create a fresh SQLite database and registered export.
Use a new directory per independent run. Only Python standard library is required.
holds.json protects the original O21 order inside restricted storage; it does not protect customer contact fields
or authorize keeping that order in the public export. Preserve only the subject key needed for the retained FK.
The ordinary export is a registered replica and must suppress all erased subjects, including restricted records.
All sources use the same explicit subject_id; aliases/discovery/adapters are outside scope.
No deletion plan, execution logic, expected cleaned data or recovery implementation is supplied.
