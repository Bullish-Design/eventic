"""Write/read orchestration — the canonical pipeline (CONCEPT §5–6).

``commit_version`` walks construct → validate → before_commit → encode →
persist → after_commit → emit → deliver, dispatching each stage to the
record class's assembled seam providers (defaults first, plugins later).
"""
