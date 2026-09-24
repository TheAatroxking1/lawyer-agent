# Token-window embedding compatibility fix

Approved scope: resolve measured 512-token truncation for full-corpus publication without changing persisted chunks, IDs, BM25 content, parser identity, or output row count.

- Strict logical model reference suffix `#token-window-mean-v1`; shared parser used by the existing provider constructor (all build/query/smoke composition already shares that constructor). Unknown fragments reject before model loading. Real model path remains separate internally.
- v1: 512 tokens including special tokens, no overlap, exact consecutive original-character spans, normalized window vectors averaged equally then normalized again. Short input fitting the token budget retains its original encode result. Require model limit512 and no default prompt; unsupported preprocessing fails closed.
- Derive safe prefix spans by actual nontruncating tokenizer counts; never decode/reconstruct text or use ambiguous substring locations. Every accepted span is rechecked, concatenation is original text, never cross inputs. Bounded windows encode in microbatches<=8 inside the existing single runner task; do not expand all input windows/vectors in memory.
- TDD: parser/unknown profile, Unicode/special-token boundary/whitespace/repeated text/long tail, fixed pooling, short compatibility, late-window failure, microbatch and output cardinality. Existing timeout/cancel/busy lifecycle tests remain required.
- Validate with unit/Ruff/mypy plus fixed real CPU/GPU samples: short-vector compatibility and long-tail participation, finite1792/L2, timing. Re-run quality check using new logical model_ref; old receipts do not match.
- Independent review by another existing task agent before full-corpus publication. No dynamic profile framework, new database fields or max_seq_length override.


Implementation verification:
- Initial RED: missing new module; GREEN 13 focused tests. Expanded adversarial cases to25.
- Independent review found raw tokenizer / actual preprocessing mismatch; observed RED (DID NOT RAISE) then added strict actual model.tokenize token-ID and mask comparison before every encode microbatch. This also rejects malformed rows/types and silent truncation.
- 129 related tests passed; focused Ruff and mypy passed. Existing build/API/smoke callers already share the constructor, which now applies the strict logical-ref parser, so no composition rewiring was necessary.
- Final GPU real64: 2.1267s,30.09rows/s; short-vector maxdiff0; tail-only derived probe legacy maxdiff0 vs new0.0032165; every actual window<=512; close drained. CPU final real64:64.1932s,0.997rows/s; short maxdiff0; legacy tail maxdiff0 vs new0.0032165338; all windows<=512 and close drained. Both reports include final source hashes and fixed sample IDs/hashes.
- Offline real tokenizer Unicode/whitespace/repetition/English cases preserved exact original-character concatenation, including cases whose characters and tokens differ substantially. A token covering unknown characters does not certify legal-semantic quality.
