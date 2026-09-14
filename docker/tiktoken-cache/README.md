# LightRAG startup tiktoken cache

The release image constructs the default `gpt-4o-mini` tokenizer at API
startup. These two cache entries are the `tiktoken` URL-keyed BPE files for
`o200k_base` and `cl100k_base`, committed so Kubernetes startup never blocks on
`https://openaipublic.blob.core.windows.net/encodings/...`.

The filename is `sha1(url)` and the content SHA-256 is checked by
`tests/setup/test_docker_base_images.py` against the hash embedded in
`tiktoken_ext.openai_public`.
