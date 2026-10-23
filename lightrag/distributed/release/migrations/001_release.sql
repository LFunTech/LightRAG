CREATE SCHEMA IF NOT EXISTS lightrag_release;
REVOKE ALL ON SCHEMA lightrag_release FROM PUBLIC;

CREATE TABLE lightrag_release.scopes (
    scope text PRIMARY KEY,
    profile_hash text NOT NULL,
    config_hash text NOT NULL,
    protocol_version integer NOT NULL DEFAULT 1 CHECK (protocol_version = 1),
    active_slot text NOT NULL CHECK (active_slot IN ('blue','green')),
    candidate_slot text CHECK (candidate_slot IN ('blue','green')),
    epoch bigint NOT NULL DEFAULT 1 CHECK (epoch > 0),
    owner text,
    phase text NOT NULL DEFAULT 'IDLE' CHECK (phase IN ('IDLE','PREPARED','DRAINING','READ_SWITCHED','WRITER_SWITCHED','RETIRING','COMPLETED','ABORTED','ABORTING','ROLLBACK_DRAINING','ROLLBACK_WRITER_SWITCHED','ROLLBACK_RETIRING','ROLLED_BACK')),
    gate_open boolean NOT NULL DEFAULT true,
    policy jsonb NOT NULL,
    identity jsonb,
    drain_deadline timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lightrag_release.releases (
    scope text NOT NULL REFERENCES lightrag_release.scopes(scope),
    release_id text NOT NULL,
    identity jsonb NOT NULL,
    phase text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}',
    previous jsonb NOT NULL DEFAULT '{}',
    rollback_approval jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope, release_id)
);
CREATE TABLE lightrag_release.instances (
    scope text NOT NULL REFERENCES lightrag_release.scopes(scope),
    instance_id text NOT NULL,
    slot text NOT NULL CHECK (slot IN ('blue','green')),
    config_hash text NOT NULL,
    profile_hash text NOT NULL,
    coordinator_owner uuid NOT NULL,
    retiring boolean NOT NULL DEFAULT false,
    closed boolean NOT NULL DEFAULT false,
    status jsonb NOT NULL DEFAULT '{}',
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope, instance_id)
);
CREATE INDEX release_instances_live ON lightrag_release.instances(scope,slot) WHERE NOT closed;
CREATE TABLE lightrag_release.commands (
    id uuid PRIMARY KEY,
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    scope text NOT NULL REFERENCES lightrag_release.scopes(scope),
    principal_id text NOT NULL,
    idempotency_key text NOT NULL,
    payload_hash text NOT NULL,
    envelope jsonb NOT NULL,
    state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED','EXECUTING','SUCCEEDED','FAILED','UNCERTAIN')),
    executor text,
    epoch bigint,
    ticket_id uuid,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (scope, principal_id, idempotency_key)
);
CREATE INDEX release_command_queue ON lightrag_release.commands(scope, sequence) WHERE state='QUEUED';
CREATE INDEX release_command_unfinished ON lightrag_release.commands(scope, state) WHERE state IN ('QUEUED','EXECUTING','UNCERTAIN');
CREATE INDEX release_command_refs ON lightrag_release.commands USING gin ((envelope->'source_refs')) WHERE state IN ('QUEUED','EXECUTING','UNCERTAIN');
CREATE TABLE lightrag_release.source_cleanup (
    scope text NOT NULL REFERENCES lightrag_release.scopes(scope),
    source_ref text NOT NULL,
    instance_id text NOT NULL,
    ticket_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('CLEANING','CLEANED')),
    PRIMARY KEY (scope,source_ref)
);
CREATE INDEX release_source_cleaning ON lightrag_release.source_cleanup(scope) WHERE state='CLEANING';
CREATE TABLE lightrag_release.tickets (
    id uuid PRIMARY KEY,
    scope text NOT NULL REFERENCES lightrag_release.scopes(scope),
    instance_id text NOT NULL,
    slot text NOT NULL CHECK (slot IN ('blue','green')),
    epoch bigint NOT NULL,
    command_id uuid REFERENCES lightrag_release.commands(id),
    kind text NOT NULL,
    state text NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE','COMPLETED','UNCERTAIN')),
    operation_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX release_ticket_unfinished ON lightrag_release.tickets(scope, instance_id) WHERE state <> 'COMPLETED';
CREATE INDEX release_ticket_operation ON lightrag_release.tickets(operation_id) WHERE state <> 'COMPLETED';
CREATE TABLE lightrag_release.schema_migrations (
    version integer PRIMARY KEY,
    checksum text NOT NULL,
    structure_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON ALL TABLES IN SCHEMA lightrag_release FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA lightrag_release FROM PUBLIC;

CREATE FUNCTION lightrag_release.register_instance(p_scope text,p_instance text,p_slot text,p_config text,p_profile text,p_owner uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE s lightrag_release.scopes; i lightrag_release.instances;
BEGIN
    SELECT * INTO STRICT s FROM lightrag_release.scopes WHERE scope=p_scope FOR UPDATE;
    IF p_profile<>s.profile_hash OR p_slot NOT IN ('blue','green') OR
       p_config IS DISTINCT FROM (CASE WHEN p_slot=s.active_slot THEN s.config_hash ELSE coalesce(s.identity->>'config_hash','') END) THEN
        RAISE EXCEPTION 'profile conflict' USING ERRCODE='P0001';
    END IF;
    INSERT INTO lightrag_release.instances(scope,instance_id,slot,config_hash,profile_hash,coordinator_owner)
    VALUES(p_scope,p_instance,p_slot,p_config,p_profile,p_owner) ON CONFLICT DO NOTHING;
    SELECT * INTO STRICT i FROM lightrag_release.instances WHERE scope=p_scope AND instance_id=p_instance;
    IF i.slot<>p_slot OR i.config_hash<>p_config OR i.coordinator_owner<>p_owner OR i.closed THEN
        RAISE EXCEPTION 'instance conflict' USING ERRCODE='P0001';
    END IF;
END; $$;

CREATE FUNCTION lightrag_release.admit_writer(p_scope text,p_instance text,p_epoch bigint,p_id uuid,p_kind text,p_command uuid,p_parent uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE s lightrag_release.scopes; i lightrag_release.instances; parent_valid boolean;
BEGIN
    SELECT * INTO STRICT s FROM lightrag_release.scopes WHERE scope=p_scope FOR UPDATE;
    SELECT * INTO STRICT i FROM lightrag_release.instances WHERE scope=p_scope AND instance_id=p_instance;
    IF i.closed OR i.retiring OR i.slot<>s.active_slot OR p_epoch<>s.epoch THEN RETURN NULL; END IF;
    SELECT EXISTS(SELECT 1 FROM lightrag_release.tickets WHERE id=p_parent AND scope=p_scope AND instance_id=p_instance AND epoch=p_epoch AND state='ACTIVE') INTO parent_valid;
    IF NOT parent_valid AND (NOT s.gate_open OR
       EXISTS(SELECT 1 FROM lightrag_release.tickets WHERE scope=p_scope AND state='UNCERTAIN') OR
       EXISTS(SELECT 1 FROM lightrag_release.commands WHERE scope=p_scope AND state='UNCERTAIN')) THEN RETURN NULL; END IF;
    INSERT INTO lightrag_release.tickets(id,scope,instance_id,slot,epoch,command_id,kind)
    VALUES(p_id,p_scope,p_instance,i.slot,p_epoch,p_command,p_kind);
    RETURN p_id;
END; $$;

CREATE FUNCTION lightrag_release.attach_ticket(p_scope text,p_instance text,p_ticket uuid,p_operation uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    UPDATE lightrag_release.tickets SET operation_id=p_operation
    WHERE id=p_ticket AND scope=p_scope AND instance_id=p_instance AND state='ACTIVE' AND (operation_id IS NULL OR operation_id=p_operation);
    IF NOT FOUND THEN RAISE EXCEPTION 'ticket conflict' USING ERRCODE='P0001'; END IF;
END; $$;

CREATE FUNCTION lightrag_release.finish_ticket(p_scope text,p_instance text,p_ticket uuid,p_uncertain boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    UPDATE lightrag_release.tickets SET state=CASE WHEN p_uncertain THEN 'UNCERTAIN' ELSE 'COMPLETED' END
    WHERE id=p_ticket AND scope=p_scope AND instance_id=p_instance AND state='ACTIVE';
    IF NOT FOUND AND NOT EXISTS(SELECT 1 FROM lightrag_release.tickets WHERE id=p_ticket AND scope=p_scope AND instance_id=p_instance AND state=CASE WHEN p_uncertain THEN 'UNCERTAIN' ELSE 'COMPLETED' END) THEN
        RAISE EXCEPTION 'ticket conflict' USING ERRCODE='P0001';
    END IF;
END; $$;

CREATE FUNCTION lightrag_release.enqueue_command(p_scope text,p_instance text,p_id uuid,p_principal text,p_key text,p_hash text,p_envelope jsonb) RETURNS lightrag_release.commands
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE s lightrag_release.scopes; c lightrag_release.commands; n bigint;
BEGIN
    SELECT * INTO STRICT s FROM lightrag_release.scopes WHERE scope=p_scope FOR UPDATE;
    IF NOT EXISTS(SELECT 1 FROM lightrag_release.instances WHERE scope=p_scope AND instance_id=p_instance AND NOT closed) OR
       (p_envelope->>'scope') IS DISTINCT FROM p_scope OR (p_envelope->'principal'->>'id') IS DISTINCT FROM p_principal OR (p_envelope->>'payload_hash') IS DISTINCT FROM p_hash OR (p_envelope->>'idempotency_key') IS DISTINCT FROM p_key THEN
        RAISE EXCEPTION 'command identity conflict' USING ERRCODE='P0001';
    END IF;
    SELECT * INTO c FROM lightrag_release.commands WHERE scope=p_scope AND principal_id=p_principal AND idempotency_key=p_key;
    IF FOUND THEN
        IF c.payload_hash<>p_hash THEN RAISE EXCEPTION 'command payload conflict' USING ERRCODE='P0001'; END IF;
        RETURN c;
    END IF;
    IF EXISTS(SELECT 1 FROM lightrag_release.source_cleanup r WHERE r.scope=p_scope AND (p_envelope->'source_refs') ? r.source_ref) THEN
        RAISE EXCEPTION 'source cleanup already owns this reference' USING ERRCODE='P0001';
    END IF;
    SELECT count(*) INTO n FROM lightrag_release.commands WHERE scope=p_scope AND state IN ('QUEUED','EXECUTING','UNCERTAIN');
    IF n>=(s.policy->>'queue_capacity')::bigint OR octet_length(p_envelope::text)>coalesce((s.policy->>'command_byte_limit')::bigint,1048576) THEN
        RAISE EXCEPTION 'queue full' USING ERRCODE='P0002';
    END IF;
    INSERT INTO lightrag_release.commands(id,scope,principal_id,idempotency_key,payload_hash,envelope)
    VALUES(p_id,p_scope,p_principal,p_key,p_hash,p_envelope) RETURNING * INTO c;
    RETURN c;
END; $$;

CREATE FUNCTION lightrag_release.claim_command(p_scope text,p_instance text,p_epoch bigint,p_ticket uuid) RETURNS lightrag_release.commands
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE s lightrag_release.scopes; c lightrag_release.commands; t uuid;
BEGIN
    SELECT * INTO STRICT s FROM lightrag_release.scopes WHERE scope=p_scope FOR UPDATE;
    IF NOT s.gate_open OR p_epoch<>s.epoch THEN RETURN NULL; END IF;
    SELECT * INTO c FROM lightrag_release.commands WHERE scope=p_scope AND state='QUEUED' ORDER BY sequence LIMIT 1 FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    t:=lightrag_release.admit_writer(p_scope,p_instance,p_epoch,p_ticket,c.envelope->>'operation',c.id,NULL);
    IF t IS NULL THEN RETURN NULL; END IF;
    UPDATE lightrag_release.commands SET state='EXECUTING',executor=p_instance,epoch=p_epoch,ticket_id=t,updated_at=clock_timestamp() WHERE id=c.id RETURNING * INTO c;
    RETURN c;
END; $$;

CREATE FUNCTION lightrag_release.finish_command(p_scope text,p_instance text,p_id uuid,p_state text,p_result jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE c lightrag_release.commands;
BEGIN
    IF p_state NOT IN ('SUCCEEDED','FAILED','UNCERTAIN') THEN RAISE EXCEPTION 'terminal state required' USING ERRCODE='P0001'; END IF;
    SELECT * INTO STRICT c FROM lightrag_release.commands WHERE id=p_id AND scope=p_scope FOR UPDATE;
    IF c.executor<>p_instance OR c.state<>'EXECUTING' THEN RAISE EXCEPTION 'executor conflict' USING ERRCODE='P0001'; END IF;
    UPDATE lightrag_release.commands SET state=p_state,result=p_result,updated_at=clock_timestamp() WHERE id=p_id;
    PERFORM lightrag_release.finish_ticket(p_scope,p_instance,c.ticket_id,p_state='UNCERTAIN');
END; $$;

CREATE FUNCTION lightrag_release.assert_ticket(p_scope text,p_instance text,p_ticket uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM lightrag_release.tickets t JOIN lightrag_release.scopes s USING(scope)
        JOIN lightrag_release.instances i ON i.scope=t.scope AND i.instance_id=t.instance_id
        WHERE t.id=p_ticket AND t.scope=p_scope AND t.instance_id=p_instance AND t.state='ACTIVE' AND t.epoch=s.epoch AND NOT i.closed) THEN
        RAISE EXCEPTION 'writer ticket is not live' USING ERRCODE='P0001';
    END IF;
END; $$;

CREATE FUNCTION lightrag_release.assert_instance_quiescent(p_scope text,p_instance text,p_status jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM lightrag_release.tickets WHERE scope=p_scope AND instance_id=p_instance AND state<>'COMPLETED') OR
       EXISTS(SELECT 1 FROM lightrag_coordination.operations o JOIN lightrag_release.instances i ON i.coordinator_owner=o.owner_id
              WHERE i.scope=p_scope AND i.instance_id=p_instance AND o.state='active') OR
       p_status IS DISTINCT FROM '{"http":0,"queries":0,"writers":0,"waiters":0,"backend":0,"background":0}'::jsonb THEN
        RAISE EXCEPTION 'instance has unfinished work' USING ERRCODE='P0001';
    END IF;
END; $$;

CREATE FUNCTION lightrag_release.close_instance(p_scope text,p_instance text,p_status jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    PERFORM lightrag_release.assert_instance_quiescent(p_scope,p_instance,p_status);
    UPDATE lightrag_release.instances SET closed=true,retiring=true,status=p_status,observed_at=clock_timestamp() WHERE scope=p_scope AND instance_id=p_instance;
    IF NOT FOUND THEN RAISE EXCEPTION 'instance is not registered' USING ERRCODE='P0001'; END IF;
END; $$;

CREATE FUNCTION lightrag_release.claim_source_cleanup(p_scope text,p_instance text,p_ticket uuid,p_ref text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    PERFORM 1 FROM lightrag_release.scopes WHERE scope=p_scope FOR UPDATE;
    PERFORM lightrag_release.assert_ticket(p_scope,p_instance,p_ticket);
    IF EXISTS(SELECT 1 FROM lightrag_release.commands WHERE scope=p_scope AND state IN ('QUEUED','EXECUTING','UNCERTAIN') AND (envelope->'source_refs') ? p_ref) THEN RETURN false; END IF;
    INSERT INTO lightrag_release.source_cleanup(scope,source_ref,instance_id,ticket_id,state) VALUES(p_scope,p_ref,p_instance,p_ticket,'CLEANING') ON CONFLICT DO NOTHING;
    RETURN FOUND;
END; $$;

CREATE FUNCTION lightrag_release.finish_source_cleanup(p_scope text,p_instance text,p_ticket uuid,p_ref text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    PERFORM lightrag_release.assert_ticket(p_scope,p_instance,p_ticket);
    UPDATE lightrag_release.source_cleanup SET state='CLEANED' WHERE scope=p_scope AND source_ref=p_ref AND instance_id=p_instance AND ticket_id=p_ticket;
    IF NOT FOUND THEN RAISE EXCEPTION 'source cleanup conflict' USING ERRCODE='P0001'; END IF;
END; $$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA lightrag_release FROM PUBLIC;
