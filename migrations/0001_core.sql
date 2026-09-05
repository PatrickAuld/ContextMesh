CREATE TABLE tenants (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    active_graph uuid,
    security_epoch bigint NOT NULL DEFAULT 0
);
CREATE TABLE principals (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    subject text NOT NULL,
    groups text[] NOT NULL DEFAULT '{}',
    disabled boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, subject)
);
CREATE TABLE agent_tokens (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    token_hash text NOT NULL UNIQUE,
    owner text NOT NULL,
    name text NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, owner) REFERENCES principals(tenant_id, subject)
);
CREATE TABLE graphs (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    name text NOT NULL,
    config jsonb NOT NULL,
    backfill_cursor bigint NOT NULL DEFAULT 0,
    backfill_target bigint NOT NULL DEFAULT 0,
    state text NOT NULL DEFAULT 'building' CHECK (state IN ('building','ready','archived')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);
ALTER TABLE tenants ADD CONSTRAINT active_graph_fk FOREIGN KEY (id, active_graph) REFERENCES graphs(tenant_id, id);
CREATE TABLE events (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    source text NOT NULL,
    external_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    actor text NOT NULL,
    agent_id uuid,
    body text,
    context jsonb NOT NULL DEFAULT '{}',
    input_hash text NOT NULL,
    classification text NOT NULL CHECK (classification IN ('internal','restricted')),
    read_groups text[] NOT NULL DEFAULT '{}',
    current boolean NOT NULL DEFAULT true,
    redacted boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id, source, external_id, revision)
);
CREATE UNIQUE INDEX events_current ON events(tenant_id,source,external_id) WHERE current;
CREATE INDEX events_scan ON events(tenant_id, sequence) WHERE current AND NOT redacted;
CREATE TABLE jobs (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    graph_id uuid NOT NULL,
    event_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','done','failed','cancelled')),
    attempts integer NOT NULL DEFAULT 0,
    lease_id uuid,
    lease_until timestamptz,
    available_at timestamptz NOT NULL DEFAULT now(),
    error_code text,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,graph_id,event_id),
    FOREIGN KEY (tenant_id,graph_id) REFERENCES graphs(tenant_id,id),
    FOREIGN KEY (tenant_id,event_id) REFERENCES events(tenant_id,id)
);
CREATE INDEX jobs_claim ON jobs(tenant_id,available_at) WHERE state IN ('pending','running');
CREATE TABLE claims (
    tenant_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    id uuid NOT NULL,
    event_id uuid NOT NULL,
    body text NOT NULL,
    quote text NOT NULL,
    intent text NOT NULL,
    entities text[] NOT NULL,
    applies jsonb NOT NULL DEFAULT '{}',
    dependencies jsonb NOT NULL DEFAULT '{}',
    slot text,
    search tsvector GENERATED ALWAYS AS (to_tsvector('english',body)) STORED,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,graph_id,id),
    FOREIGN KEY (tenant_id,graph_id) REFERENCES graphs(tenant_id,id),
    FOREIGN KEY (tenant_id,event_id) REFERENCES events(tenant_id,id)
);
CREATE INDEX claims_text ON claims USING gin(search);
CREATE INDEX claims_entities ON claims USING gin(entities);
CREATE INDEX claims_graph ON claims(tenant_id,graph_id,event_id);
CREATE TABLE edges (
    tenant_id uuid NOT NULL,
    graph_id uuid NOT NULL,
    claim_id uuid NOT NULL,
    from_entity text NOT NULL,
    relation text NOT NULL,
    to_entity text NOT NULL,
    PRIMARY KEY (tenant_id,claim_id,from_entity,relation,to_entity),
    FOREIGN KEY (tenant_id,graph_id,claim_id) REFERENCES claims(tenant_id,graph_id,id) ON DELETE CASCADE
);
CREATE INDEX edges_walk ON edges(tenant_id,graph_id,from_entity);
CREATE TABLE policies (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    name text NOT NULL,
    purpose text NOT NULL,
    audiences text[] NOT NULL,
    instruction text NOT NULL,
    outputs jsonb NOT NULL,
    evidence uuid[] NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,id)
);
CREATE TABLE receipts (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    actor text NOT NULL,
    agent_id uuid,
    graph_id uuid NOT NULL,
    query_hash text NOT NULL,
    claim_ids uuid[] NOT NULL,
    policy_ids uuid[] NOT NULL,
    epoch bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,id)
);
CREATE TABLE audit (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    actor text NOT NULL,
    agent_id uuid,
    action text NOT NULL,
    target uuid,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,sequence)
);
CREATE FUNCTION forbid_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit entries are append only'; END $$;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON audit FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation();
DO $$ DECLARE tab text; BEGIN
    FOREACH tab IN ARRAY ARRAY['principals','agent_tokens','graphs','events','jobs','claims','edges','policies','receipts','audit'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tab);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',tab);
        EXECUTE format('CREATE POLICY tenant_boundary ON %I USING (tenant_id = nullif(current_setting(''app.tenant_id'',true),'''')::uuid) WITH CHECK (tenant_id = nullif(current_setting(''app.tenant_id'',true),'''')::uuid)',tab);
    END LOOP;
END $$;

CREATE FUNCTION guard_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.tenant_id,NEW.id,NEW.source,NEW.external_id,NEW.revision,NEW.actor,NEW.agent_id,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.source,OLD.external_id,OLD.revision,OLD.actor,OLD.agent_id,OLD.created_at) THEN
        RAISE EXCEPTION 'source identity is immutable';
    END IF;
    IF OLD.redacted AND NOT NEW.redacted THEN RAISE EXCEPTION 'redaction is irreversible'; END IF;
    IF ROW(NEW.body,NEW.context,NEW.input_hash) IS DISTINCT FROM ROW(OLD.body,OLD.context,OLD.input_hash)
       AND NOT (NEW.redacted AND NEW.body IS NULL AND NEW.context='{}'::jsonb AND NEW.input_hash='') THEN
        RAISE EXCEPTION 'correct evidence with a new revision';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER evidence_guard BEFORE UPDATE ON events FOR EACH ROW EXECUTE FUNCTION guard_evidence();
