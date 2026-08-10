# K1-S4: Related panel

One lithos_related(id, depth=1) call → outgoing links, back-links (incoming),
provenance (sources/derived/unresolved), and typed edges with type + weight;
endpoint titles via a per-request-cached lithos_read(max_length=1) fan-out
capped at related_title_fanout_cap (default 20). Acceptance: back-links
section lists incoming link titles; 25 edges with cap 20 renders '+5 more'.
PRD slice 4.
