# AIIDE-Builder_Agent #
=======================

Python-Document Splitter zum Chunken von Code.

Ziel

Das gesamte Repo wird als Repository-Wissen in die Multi-Model-AgentsDB ueberfuehrt,
damit der IDE-Agent lokal auf modulbezogenen Kontext zugreifen kann.

Pipeline

- Jedes Python-Modul wird mit `repo_code_splitter.py` AST-basiert zerlegt.
- Es entstehen Block-Segmente fuer Modul-Doku, Imports, Klassen, Funktionen und Rest-Code.
- Pro Modul wird ein parser-kompatibles Payload erzeugt.
- Dieses Payload laeuft durch `ObjectMappingService` und erzeugt `document`, `entity` und `relation` Objekte.
- Danach werden alle Owner-Typen embedded: `block`, `entity`, `relation`.

Runtime Tool

- Tool-Name: `repo_knowledge_worker`
- Operationen: `scan`, `build`, `cleanup`, `delete`, `rebuild`, `repair_namespace`, `status`
- Default-Ziel: Python-Module (`.py`) im aktuellen Repo

Beispiel

```python
repo_knowledge_worker(
	operation="build",
	root_dir="/abs/path/to/repo",
	extensions=[".py"],
	workers=4,
)

# Async rebuild with cleanup in one job
job = repo_knowledge_worker(
	operation="repair_namespace",
	run_async=True,
	root_dir="/abs/path/to/repo",
	extensions=[".py"],
	workers=4,
)

# Poll status
repo_knowledge_worker(operation="status", job_id=job.get("job_id"))
```

Erwartetes Ergebnis

- Repository-Wissen liegt in der AgentsDB-Schemaform vor.
- Der IDE-Agent kann Modul-, Klassen-, Funktions- und Dependency-Kontext ueber dieselbe Knowledge-Pipeline konsumieren.
- Das System bleibt kompatibel zu bestehender Parser-/Mapping-/Embedding-Logik.