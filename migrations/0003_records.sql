DROP TABLE edges;
DROP TABLE claims;
DROP TABLE receipts;
DROP TABLE jobs;
DROP TABLE events;
ALTER TABLE tenants DROP CONSTRAINT active_graph_fk;
ALTER TABLE tenants DROP COLUMN active_graph;
DROP TABLE graphs;

CREATE TABLE records (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    author text NOT NULL,
    agent_id uuid,
    content text NOT NULL,
    scope jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'object'),
    inputs uuid[] NOT NULL DEFAULT '{}' CHECK (cardinality(inputs) <= 256),
    supports jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(supports) = 'array' AND jsonb_array_length(supports) <= 256),
    supersedes uuid[] NOT NULL DEFAULT '{}' CHECK (cardinality(supersedes) <= 256),
    metadata jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 64000),
    derivation jsonb CHECK (derivation IS NULL OR jsonb_typeof(derivation) = 'object'),
    input_hash text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    redacted boolean NOT NULL DEFAULT false,
    search tsvector GENERATED ALWAYS AS (to_tsvector('simple',content)) STORED,
    CHECK (redacted OR octet_length(content) BETWEEN 1 AND 256000),
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,sequence),
    FOREIGN KEY (tenant_id,author) REFERENCES principals(tenant_id,subject)
);
CREATE INDEX records_sequence ON records(tenant_id,sequence) WHERE NOT redacted;
CREATE INDEX records_scope ON records USING gin(scope);
CREATE INDEX records_inputs ON records USING gin(inputs);
CREATE INDEX records_supersedes ON records USING gin(supersedes);
CREATE INDEX records_search ON records USING gin(search);

CREATE TABLE record_links (
    tenant_id uuid NOT NULL,
    record_id uuid NOT NULL,
    input_id uuid NOT NULL,
    supersedes boolean NOT NULL,
    PRIMARY KEY (tenant_id,record_id,input_id),
    FOREIGN KEY (tenant_id,record_id) REFERENCES records(tenant_id,id),
    FOREIGN KEY (tenant_id,input_id) REFERENCES records(tenant_id,id),
    CHECK (record_id <> input_id)
);
CREATE INDEX record_links_ancestors ON record_links(tenant_id,input_id,record_id);

CREATE TABLE jobs (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    record_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','done','failed','cancelled')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_id uuid,
    lease_until timestamptz,
    available_at timestamptz NOT NULL DEFAULT now(),
    error_code text,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,record_id),
    FOREIGN KEY (tenant_id,record_id) REFERENCES records(tenant_id,id)
);
CREATE INDEX jobs_claim ON jobs(tenant_id,available_at) WHERE state IN ('pending','running');

ALTER TABLE records ENABLE ROW LEVEL SECURITY;
ALTER TABLE records FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_boundary ON records USING (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid);
ALTER TABLE record_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE record_links FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_boundary ON record_links USING (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid);
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_boundary ON jobs USING (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid);

CREATE FUNCTION guard_record_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'records cannot be deleted'; END IF;
    IF OLD.redacted AND NOT NEW.redacted THEN RAISE EXCEPTION 'redaction is irreversible'; END IF;
    IF ROW(NEW.tenant_id,NEW.id,NEW.sequence,NEW.author,NEW.agent_id,
           NEW.inputs,NEW.supersedes,NEW.recorded_at)
       IS DISTINCT FROM
       ROW(OLD.tenant_id,OLD.id,OLD.sequence,OLD.author,OLD.agent_id,
           OLD.inputs,OLD.supersedes,OLD.recorded_at) THEN
        RAISE EXCEPTION 'record content and identity are immutable';
    END IF;
    IF NEW.redacted AND NOT OLD.redacted AND NEW.content='' AND NEW.supports='[]'::jsonb
       AND NEW.metadata='{}'::jsonb AND NEW.derivation IS NULL AND NEW.input_hash='' THEN
        RETURN NEW;
    END IF;
    IF ROW(NEW.content,NEW.supports,NEW.metadata,NEW.derivation,NEW.input_hash)
       IS DISTINCT FROM ROW(OLD.content,OLD.supports,OLD.metadata,OLD.derivation,OLD.input_hash) THEN
        RAISE EXCEPTION 'record payload is immutable';
    END IF;
    IF (NEW.scope - 'visibility' - 'groups') IS DISTINCT FROM (OLD.scope - 'visibility' - 'groups') THEN
        RAISE EXCEPTION 'record scope identity is immutable';
    END IF;
    IF NEW.redacted = OLD.redacted AND NEW.scope IS DISTINCT FROM OLD.scope THEN
        RETURN NEW;
    END IF;
    IF NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'combine no record mutations'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER records_immutable BEFORE UPDATE OR DELETE ON records FOR EACH ROW EXECUTE FUNCTION guard_record_mutation();

CREATE FUNCTION forbid_record_link_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'record lineage is immutable'; END $$;
CREATE TRIGGER record_links_immutable BEFORE UPDATE OR DELETE ON record_links FOR EACH ROW EXECUTE FUNCTION forbid_record_link_mutation();
