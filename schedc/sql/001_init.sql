-- 001_init.sql -- schedc versioned schedule storage
-- (schema_migrations itself is created by the migration runner)
CREATE TABLE versions (
    version         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id      text        NOT NULL UNIQUE,
    content_hash    text        NOT NULL,
    window_start    timestamptz NOT NULL,
    window_end      timestamptz NOT NULL,
    rule_count      integer     NOT NULL,
    plan_count      integer     NOT NULL,
    exclusion_count integer     NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Singleton row pointing at the currently published version.
-- The row is created by this migration (version NULL = nothing published);
-- publishers take SELECT ... FOR UPDATE on it to serialise publication.
CREATE TABLE current_version (
    id      boolean PRIMARY KEY DEFAULT true CHECK (id),
    version integer REFERENCES versions(version)
);
INSERT INTO current_version (id, version) VALUES (true, NULL);

CREATE TABLE rules (
    version integer NOT NULL REFERENCES versions(version),
    rule_id text    NOT NULL,
    content jsonb   NOT NULL,
    PRIMARY KEY (version, rule_id)
);

CREATE TABLE rule_exceptions (
    version      integer NOT NULL REFERENCES versions(version),
    rule_id      text    NOT NULL,
    local_date   date    NOT NULL,
    action       text    NOT NULL CHECK (action IN ('skip', 'replace')),
    replace_time time,
    PRIMARY KEY (version, rule_id, local_date)
);

CREATE TABLE plans (
    version         integer   NOT NULL REFERENCES versions(version),
    plan_id         text      NOT NULL,
    rule_id         text      NOT NULL,
    planned_date    date      NOT NULL,  -- original planned local date
    local_time      timestamp NOT NULL,  -- final local time (naive)
    utc_time        timestamptz NOT NULL,
    offset_seconds  integer   NOT NULL,
    branch          text      NOT NULL CHECK (branch IN ('none', 'first', 'second')),
    exception_basis text,
    PRIMARY KEY (version, plan_id),
    UNIQUE (version, rule_id, planned_date, utc_time)
);
CREATE INDEX plans_version_utc ON plans (version, utc_time, rule_id, planned_date);

CREATE TABLE exclusions (
    version    integer NOT NULL REFERENCES versions(version),
    rule_id    text    NOT NULL,
    local_date date    NOT NULL,
    reason     text    NOT NULL,
    detail     text    NOT NULL
);
