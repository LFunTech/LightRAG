-- Immutable production storage baseline v1. Add a new migration for changes.

CREATE TABLE LIGHTRAG_DOC_FULL (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    doc_name VARCHAR(1024),
                    content TEXT,
                    meta JSONB,
                    sidecar_location TEXT NULL,
                    parse_format VARCHAR(32) NULL DEFAULT 'raw',
                    -- content_hash is TEXT (not VARCHAR(N)) so the column is
                    -- agnostic to the hash algorithm. Today's pipeline writes
                    -- 64-char SHA-256 hex; future algos (SHA-512, base64) do
                    -- not require a schema change.
                    content_hash TEXT NULL,
                    -- process_options is an opaque selector string emitted by
                    -- sanitize_process_options() (e.g. "Fi").
                    process_options TEXT NULL,
                    chunk_options JSONB NULL DEFAULT '{}'::jsonb,
                    parse_engine TEXT NULL,
                    object_source JSONB NULL DEFAULT '{}'::jsonb,
                    create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
	                CONSTRAINT LIGHTRAG_DOC_FULL_PK PRIMARY KEY (workspace, id)
                    );

CREATE TABLE LIGHTRAG_DOC_CHUNKS (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    full_doc_id VARCHAR(256),
                    chunk_order_index INTEGER,
                    tokens INTEGER,
                    content TEXT,
                    file_path TEXT NULL,
                    llm_cache_list JSONB NULL DEFAULT '[]'::jsonb,
                    heading JSONB NULL DEFAULT '{}'::jsonb,
                    sidecar JSONB NULL DEFAULT '{}'::jsonb,
                    create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
	                CONSTRAINT LIGHTRAG_DOC_CHUNKS_PK PRIMARY KEY (workspace, id)
                    );

CREATE TABLE LIGHTRAG_LLM_CACHE (
	                workspace varchar(255) NOT NULL,
	                id varchar(255) NOT NULL,
                    original_prompt TEXT,
                    return_value TEXT,
                    chunk_id VARCHAR(255) NULL,
                    cache_type VARCHAR(32),
                    queryparam JSONB NULL,
                    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	                CONSTRAINT LIGHTRAG_LLM_CACHE_PK PRIMARY KEY (workspace, id)
                    );

CREATE TABLE LIGHTRAG_UPLOAD_SESSIONS (
	                workspace varchar(255) NOT NULL,
	                id varchar(255) NOT NULL,
                    session JSONB NOT NULL,
                    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	                CONSTRAINT LIGHTRAG_UPLOAD_SESSIONS_PK PRIMARY KEY (workspace, id)
                    );

CREATE TABLE LIGHTRAG_DOC_STATUS (
	               workspace varchar(255) NOT NULL,
	               id varchar(255) NOT NULL,
	               content_summary varchar(255) NULL,
	               content_length int4 NULL,
	               chunks_count int4 NULL,
	               status varchar(64) NULL,
	               file_path TEXT NULL,
	               chunks_list JSONB NULL DEFAULT '[]'::jsonb,
	               track_id varchar(255) NULL,
	               metadata JSONB NULL DEFAULT '{}'::jsonb,
	               error_msg TEXT NULL,
	               -- content_hash is TEXT (not VARCHAR(N)) so the column is
	               -- agnostic to the hash algorithm. Today's pipeline writes
	               -- 64-char SHA-256 hex; future algos (SHA-512, base64) do
	               -- not require a schema change.
	               content_hash TEXT NULL,
	               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
	               CONSTRAINT LIGHTRAG_DOC_STATUS_PK PRIMARY KEY (workspace, id)
	              );

CREATE TABLE LIGHTRAG_FULL_ENTITIES (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    entity_names JSONB,
                    count INTEGER,
                    create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT LIGHTRAG_FULL_ENTITIES_PK PRIMARY KEY (workspace, id)
                    );

CREATE TABLE LIGHTRAG_FULL_RELATIONS (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    relation_pairs JSONB,
                    count INTEGER,
                    create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT LIGHTRAG_FULL_RELATIONS_PK PRIMARY KEY (workspace, id)
                    );

CREATE TABLE LIGHTRAG_ENTITY_CHUNKS (
                    id VARCHAR(512),
                    workspace VARCHAR(255),
                    chunk_ids JSONB,
                    count INTEGER,
                    create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT LIGHTRAG_ENTITY_CHUNKS_PK PRIMARY KEY (workspace, id)
                    );

CREATE TABLE LIGHTRAG_RELATION_CHUNKS (
                    id VARCHAR(512),
                    workspace VARCHAR(255),
                    chunk_ids JSONB,
                    count INTEGER,
                    create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT LIGHTRAG_RELATION_CHUNKS_PK PRIMARY KEY (workspace, id)
                    );
