import pytest


def profile_env():
    return {
        'LIGHTRAG_DEPLOYMENT_ID':'prod','WORKSPACE':'prod','POSTGRES_HOST':'pg.invalid','POSTGRES_PORT':'5432','POSTGRES_DATABASE':'prod',
        'HUGEGRAPH_URI':'http://graph.invalid:8080','HUGEGRAPH_GRAPHSPACE':'prod','HUGEGRAPH_GRAPH':'prod',
        'EMBEDDING_BINDING':'openai','EMBEDDING_BINDING_HOST':'https://model.invalid/v1','EMBEDDING_MODEL':'fixture','EMBEDDING_DIM':'8',
        'EMBEDDING_ASYMMETRIC':'false','EMBEDDING_DOCUMENT_PREFIX':'','EMBEDDING_QUERY_PREFIX':'',
        'S3_ENDPOINT_URL':'https://objects.invalid','S3_BUCKET':'prod','S3_OBJECT_PREFIX':'prod/objects',
    }


def test_profile_binds_physical_embedding_endpoint_and_both_prefixes():
    from lightrag_delivery.profile import physical_profile_hash
    env=profile_env()
    original=physical_profile_hash(env)
    for key,value in [('EMBEDDING_BINDING_HOST','https://different.invalid'),('EMBEDDING_DOCUMENT_PREFIX','different'),('EMBEDDING_QUERY_PREFIX','different'),('S3_OBJECT_PREFIX','other')]:
        changed=dict(env,**{key:value})
        assert physical_profile_hash(changed)!=original
    del env['EMBEDDING_BINDING']
    with pytest.raises(ValueError):
        physical_profile_hash(env)
