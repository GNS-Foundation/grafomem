import re
import sys

def main():
    with open("src/aml/backends/postgres_gmp.py", "r") as f:
        content = f.read()

    # 1. Add _CANON
    content = content.replace(
        'import numpy as np\nimport psycopg',
        'import functools\nimport json\nimport numpy as np\nimport psycopg'
    )
    content = content.replace(
        'logger = logging.getLogger("grafomem.gmp.postgres")',
        'logger = logging.getLogger("grafomem.gmp.postgres")\n\n_CANON = functools.partial(json.dumps, sort_keys=True, separators=(",", ":"), default=str)'
    )

    # 2. Add columns to ensure_schema
    schema_addition = """
                # Migration for encryption columns
                try:
                    cur.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS content_enc TEXT;")
                    cur.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS metadata_enc TEXT;")
                except Exception as e:
                    logger.warning(f"Could not alter memories table for encryption columns: {e}")
"""
    content = content.replace(
        '                # Enable Postgres RLS',
        schema_addition + '\n                # Enable Postgres RLS'
    )

    # 3. Add _encrypt_memory helper method
    encrypt_helper = """    def _encrypt_memory(self, content: str, metadata: dict | None) -> tuple[str, str | None, str, str | None]:
        \"\"\"Return (db_content, enc_content, db_metadata, enc_metadata)\"\"\"
        meta_canon = _CANON(metadata) if metadata else "{}"
        if self._encryption:
            enc_content = self._encryption.encrypt(content)
            db_content = "[ENCRYPTED]"
            enc_meta = self._encryption.encrypt(meta_canon)
            db_meta = "{}"
            return db_content, enc_content, db_meta, enc_meta
        else:
            return content, None, meta_canon, None
"""
    # Insert helper before write
    content = content.replace(
        '    def write(self, content: str, options: WriteOptions) -> int:',
        encrypt_helper + '\n    def write(self, content: str, options: WriteOptions) -> int:'
    )

    # 4. write()
    content = re.sub(
        r'        meta = options\.metadata or \{\}\n        sig, pub = self\._provenance\(content, options\)\n\n        with self\._tenant_conn\(options\.tenant_id\) as \(conn, cur\):\n            cur\.execute\(\n                "INSERT INTO memories \(content, metadata, valid_from, valid_until, tenant_id, written_by, signature, public_key\) "\n                "VALUES \(%s, %s, %s, %s, %s, %s, %s, %s\) RETURNING ref",\n                \(\n                    content,\n                    psycopg\.types\.json\.Jsonb\(meta\),\n                    _vec_from\(options\.valid_from\),\n                    _vec_until\(options\.valid_until\),\n                    _vec_tenant\(options\.tenant_id\),\n                    options\.written_by,\n                    sig,\n                    pub\n                \)\n            \)',
        '        meta = options.metadata or {}\n        sig, pub = self._provenance(content, options)\n        db_content, enc_content, db_meta, enc_meta = self._encrypt_memory(content, meta)\n\n        with self._tenant_conn(options.tenant_id) as (conn, cur):\n            cur.execute(\n                "INSERT INTO memories (content, metadata, valid_from, valid_until, tenant_id, written_by, signature, public_key, content_enc, metadata_enc) "\n                "VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING ref",\n                (\n                    db_content,\n                    db_meta,\n                    _vec_from(options.valid_from),\n                    _vec_until(options.valid_until),\n                    _vec_tenant(options.tenant_id),\n                    options.written_by,\n                    sig,\n                    pub,\n                    enc_content,\n                    enc_meta\n                )\n            )',
        content
    )

    # 5. write_many()
    write_many_replace = """        for item, opts in items:
            meta = opts.metadata or {}
            sig, pub = self._provenance(item, opts)
            db_content, enc_content, db_meta, enc_meta = self._encrypt_memory(item, meta)
            params.append((
                db_content,
                db_meta,
                _vec_from(opts.valid_from),
                _vec_until(opts.valid_until),
                _vec_tenant(opts.tenant_id),
                opts.written_by,
                sig,
                pub,
                enc_content,
                enc_meta
            ))

        with self._tenant_conn(items[0][1].tenant_id) as (conn, cur):
            cur.executemany(
                "INSERT INTO memories (content, metadata, valid_from, valid_until, tenant_id, written_by, signature, public_key, content_enc, metadata_enc) "
                "VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING ref",
                params,
                returning=True
            )"""
    content = re.sub(
        r'        for item, opts in items:\n            meta = opts\.metadata or \{\}\n            sig, pub = self\._provenance\(item, opts\)\n            params\.append\(\(\n                item,\n                psycopg\.types\.json\.Jsonb\(meta\),\n                _vec_from\(opts\.valid_from\),\n                _vec_until\(opts\.valid_until\),\n                _vec_tenant\(opts\.tenant_id\),\n                opts\.written_by,\n                sig,\n                pub\n            \)\)\n\n        with self\._tenant_conn\(items\[0\]\[1\]\.tenant_id\) as \(conn, cur\):\n            cur\.executemany\(\n                "INSERT INTO memories \(content, metadata, valid_from, valid_until, tenant_id, written_by, signature, public_key\) "\n                "VALUES \(%s, %s, %s, %s, %s, %s, %s, %s\) RETURNING ref",\n                params,\n                returning=True\n            \)',
        write_many_replace,
        content
    )

    # 6. supersede()
    supersede_replace = """        meta = options.metadata or {}
        sig, pub = self._provenance(content, options)
        db_content, enc_content, db_meta, enc_meta = self._encrypt_memory(content, meta)

        with self._tenant_conn(options.tenant_id) as (conn, cur):
            cur.execute(
                "UPDATE memories SET valid_until = now() WHERE ref = %s RETURNING valid_until",
                (old_ref,)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Memory ref {old_ref} not found for supersede")

            cur.execute(
                "INSERT INTO memories (content, metadata, valid_from, valid_until, tenant_id, superseded_by, written_by, signature, public_key, content_enc, metadata_enc) "
                "VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING ref",
                (
                    db_content,
                    db_meta,
                    row[0],
                    _vec_until(options.valid_until),
                    _vec_tenant(options.tenant_id),
                    old_ref,
                    options.written_by,
                    sig,
                    pub,
                    enc_content,
                    enc_meta
                )
            )"""
    content = re.sub(
        r'        meta = options\.metadata or \{\}\n        sig, pub = self\._provenance\(content, options\)\n\n        with self\._tenant_conn\(options\.tenant_id\) as \(conn, cur\):\n            cur\.execute\(\n                "UPDATE memories SET valid_until = now\(\) WHERE ref = %s RETURNING valid_until",\n                \(old_ref,\)\n            \)\n            row = cur\.fetchone\(\)\n            if not row:\n                raise ValueError\(f"Memory ref \{old_ref\} not found for supersede"\)\n\n            cur\.execute\(\n                "INSERT INTO memories \(content, metadata, valid_from, valid_until, tenant_id, superseded_by, written_by, signature, public_key\) "\n                "VALUES \(%s, %s, %s, %s, %s, %s, %s, %s, %s\) RETURNING ref",\n                \(\n                    content,\n                    psycopg\.types\.json\.Jsonb\(meta\),\n                    row\[0\],\n                    _vec_until\(options\.valid_until\),\n                    _vec_tenant\(options\.tenant_id\),\n                    old_ref,\n                    options\.written_by,\n                    sig,\n                    pub\n                \)\n            \)',
        supersede_replace,
        content
    )

    # 7. SELECT statements to include _enc columns
    content = content.replace(
        '                "SELECT m.ref, m.content, m.metadata, m.valid_from, m.valid_until, m.tenant_id, m.written_at, m.written_by, m.signature, m.public_key, m.superseded_by "',
        '                "SELECT m.ref, m.content, m.metadata, m.valid_from, m.valid_until, m.tenant_id, m.written_at, m.written_by, m.signature, m.public_key, m.superseded_by, m.content_enc, m.metadata_enc "'
    )

    # 8. _row_to_memory
    row_to_memory_replace = """    def _row_to_memory(self, row, content_override: str | None = None) -> Memory:
        if self._encryption:
            if row.get("content_enc"):
                content = self._encryption.decrypt(row["content_enc"])
            else:
                content = row["content"]
                
            if row.get("metadata_enc"):
                meta_str = self._encryption.decrypt(row["metadata_enc"])
                metadata = json.loads(meta_str) if meta_str else {}
            else:
                metadata = row["metadata"]
        else:
            content = row["content"]
            metadata = row["metadata"]

        if content_override is not None:
            content = content_override

        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except:
                pass

        return Memory(
            ref=row["ref"],
            content=content,"""
    content = re.sub(
        r'    def _row_to_memory\(self, row, content_override: str \| None = None\) -> Memory:\n        content = content_override if content_override is not None else row\["content"\]\n        return Memory\(\n            ref=row\["ref"\],\n            content=content,',
        row_to_memory_replace,
        content
    )

    with open("src/aml/backends/postgres_gmp.py", "w") as f:
        f.write(content)

if __name__ == "__main__":
    main()
