"""prd-decompose plugin (US-12, US-13).

Reads a Pocock-shaped PRD doc from Lithos, runs Claude with a structured-output
prompt (template in ``prompt.md``, adapted from Pocock's ``to-issues`` skill),
writes one Lithos story doc per story, creates the per-PRD integration branch,
creates one Lithos task per story chained with ``blocks`` edges.

Note for whoever fills in the body: chain stories via ``task_create(depends_on=…)``,
NOT ``metadata.depends_on`` — Lithos rejects that key outright
(``invalid_metadata_key``: "task dependencies are first-class task edges"). See
``cli/_project_import_bulk.create_tasks`` for the worked equivalent.
"""
