-- Immutable vector templates; names and types are bound by the approved plan.

CREATE TABLE LIGHTRAG_VDB_CHUNKS (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    full_doc_id VARCHAR(256),
                    chunk_order_index INTEGER,
                    tokens INTEGER,
                    content TEXT,
                    content_vector VECTOR(dimension),
                    file_path TEXT NULL,
                    create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
	                CONSTRAINT LIGHTRAG_VDB_CHUNKS_PK PRIMARY KEY (workspace, id)
                    );

CREATE TABLE LIGHTRAG_VDB_ENTITY (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    entity_name VARCHAR(512),
                    content TEXT,
                    content_vector VECTOR(dimension),
                    create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    chunk_ids VARCHAR(255)[] NULL,
                    file_path TEXT NULL,
	                CONSTRAINT LIGHTRAG_VDB_ENTITY_PK PRIMARY KEY (workspace, id)
                    );

CREATE TABLE LIGHTRAG_VDB_RELATION (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    source_id VARCHAR(512),
                    target_id VARCHAR(512),
                    content TEXT,
                    content_vector VECTOR(dimension),
                    create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
                    chunk_ids VARCHAR(255)[] NULL,
                    file_path TEXT NULL,
	                CONSTRAINT LIGHTRAG_VDB_RELATION_PK PRIMARY KEY (workspace, id)
                    );
