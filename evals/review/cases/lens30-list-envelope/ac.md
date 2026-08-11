# K1-S2: Wiki-link tokenizer + resolver route

Token-splice wiki-link rendering (walk inline text tokens only, never regex
the raw markdown — code fences must stay literal); GET
/knowledge/resolve?target=&from= three-step resolver (UUID → redirect;
lithos_read(path=target+'.md') probe; else disambiguate via the source note's
lithos_related outgoing set + lithos_list(title_contains=)); disambiguation
and unresolved pages. Acceptance: the resolver decision table passes; links
inside code fences stay literal. PRD slice 2.
