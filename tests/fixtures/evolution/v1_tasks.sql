-- A database written by a schema_version=1 declaration, hand-loaded.
-- The fixture reader opens it through a schema_version=2 stream.
CREATE TABLE eventic_revision (
    revision_id TEXT PRIMARY KEY,
    stream TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    meta_version INTEGER NOT NULL,
    encoding TEXT NOT NULL,
    payload TEXT NOT NULL,
    digest TEXT NOT NULL,
    meta TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    CONSTRAINT uq_revision UNIQUE (stream, aggregate_id, revision),
    CONSTRAINT ck_revision_nonneg CHECK (revision >= 0),
    CONSTRAINT ck_kind CHECK (kind IN ('create','change')),
    CONSTRAINT ck_schema_version CHECK (schema_version >= 1),
    CONSTRAINT ck_encoding CHECK (encoding IN ('snapshot/1','delta/1')),
    CONSTRAINT ck_create_at_zero CHECK ((revision = 0) = (kind = 'create')),
    CONSTRAINT ck_stream_nonempty CHECK (stream <> '')
);

CREATE TABLE eventic_head (
    stream TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    revision_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    meta_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    digest TEXT NOT NULL,
    meta TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (stream, aggregate_id)
);

CREATE TABLE eventic_intent (
    intent_id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    queue TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    leased_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    CONSTRAINT ck_intent_queue CHECK (queue <> ''),
    CONSTRAINT ck_intent_status CHECK (status IN ('pending','leased','dead')),
    CONSTRAINT uq_intent_sub_rev UNIQUE (subscription_id, revision_id)
);

CREATE TABLE eventic_schema (
    stream TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (stream, schema_version)
);

-- aggregate 00000000-0000-0000-0000-000000000007 written as schema_version=1
INSERT INTO eventic_revision VALUES (
    '63b51a3b8f1d5ff0a9c59fd2e6a0f0f9',
    'tasks',
    '00000000000000000000000000000007',
    0,
    'create',
    1,
    1,
    'snapshot/1',
    '{"done": false, "text": "from-fixture"}',
    'b0a2e2f21c9e9e6d37e4c31a9b8e3f1b5d7e9f0a1b2c3d4e5f6a7b8c9d0e1f2',
    '{}',
    '2024-01-01 00:00:00'
);

INSERT INTO eventic_head VALUES (
    'tasks',
    '00000000000000000000000000000007',
    0,
    '63b51a3b8f1d5ff0a9c59fd2e6a0f0f9',
    1,
    1,
    '{"done": false, "text": "from-fixture"}',
    'b0a2e2f21c9e9e6d37e4c31a9b8e3f1b5d7e9f0a1b2c3d4e5f6a7b8c9d0e1f2',
    '{}',
    '2024-01-01 00:00:00'
);
