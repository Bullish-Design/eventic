# Structural Refactor Review 004

This project contains the evidence, analysis, and recommendations from an
adversarial review of Eventic after the 0.3 structural refactor. The review found
seven release blockers and concludes that the commit path needs structural repair
before 0.3 is treated as a durable event-history library.

## Scope

- Public API and conceptual model
- Module boundaries and dependency direction
- State, record, persistence, and dispatch invariants
- Transactionality, concurrency, idempotency, and failure semantics
- Extension seams, integrations, migrations, and operability
- Tests, documentation, packaging, typing, security, and maintainability

## Artifacts

- `LOG.md` — dated review activity and verification evidence
- `REVIEW.md` — invariant audit, 32 prioritized findings, target architecture,
  implementation sequence, and validation matrix
- `probes/runtime_adversarial.py` — executable runtime counterexamples
- `probes/migration_adversarial.py` — executable Alembic/schema counterexamples
