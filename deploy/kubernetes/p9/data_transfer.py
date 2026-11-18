"""Exact image and authenticated data transfer for the owned P9 site.

This module supplies stream primitives; the deployment owner checks writer fencing
and authorizes the source/target before calling restore.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess as sp
import tarfile
from pathlib import Path, PurePosixPath

NODE = "bbx-local-native-k8s-control-plane"
SHA = re.compile(r"sha256:[a-f0-9]{64}")


def checked(args, *, input_bytes=None):
    result = sp.run(args, input=input_bytes, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} command failed: {result.returncode}")
    return result.stdout


def digest_file(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def pipe(producer, consumer):
    """Check both ends, including a producer terminated on early consumer failure."""
    with sp.Popen(producer, stdout=sp.PIPE, stderr=sp.DEVNULL) as first:
        assert first.stdout is not None
        result = sp.run(consumer, stdin=first.stdout, capture_output=True)
        first.stdout.close()
        if result.returncode and first.poll() is None:
            first.terminate()
        producer_code = first.wait()
        if producer_code or result.returncode:
            raise RuntimeError(
                f"Transfer failed: source={producer_code}, destination={result.returncode}"
            )
        return result.stdout


def load_exact_image(tag, expected):
    """Import a local OCI image and verify that CRI resolves the exact manifest."""
    assert SHA.fullmatch(expected) and ":" in tag
    repo = tag.rsplit(":", 1)[0]
    repo = repo if "/" in repo else f"library/{repo}"
    alias = f"docker.io/{repo}@{expected}"
    inspect = ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", tag)
    assert f"{repo.removeprefix('library/')}@{expected}" in json.loads(checked(inspect))
    ctr = ("docker", "exec", NODE, "ctr", "--namespace=k8s.io", "images")
    if not checked((*ctr, "list", "--quiet", f"name=={alias}")).strip():
        pipe(
            ("docker", "image", "save", tag),
            (
                "docker",
                "exec",
                "-i",
                NODE,
                "ctr",
                "--namespace=k8s.io",
                "images",
                "import",
                "--platform",
                "linux/amd64",
                "--digests",
                "--label",
                "io.cri-containerd.image=managed",
                "-",
            ),
        )
    if not checked((*ctr, "list", "--quiet", f"name=={alias}")).strip():
        checked(
            (*ctr, "tag", "docker.io/" + tag if "/" in tag else "docker.io/library/" + tag, alias)
        )
    rows = checked((*ctr, "list", f"name=={alias}")).decode().splitlines()
    assert len(rows) == 2 and rows[1].split()[2] == expected
    checked((*ctr, "label", alias, "io.cri-containerd.image=managed"))
    data = json.loads(checked(("docker", "exec", NODE, "crictl", "inspecti", alias)))
    assert data["status"]["id"] and alias in data["status"]["repoDigests"]
    return {"image": alias, "cri_id": data["status"]["id"]}


def encrypt_backup(producer, target, cert):
    """Stream data into authenticated CMS; publish only a completed ciphertext."""
    target = Path(target)
    assert not target.exists() and not target.with_suffix(".pending").exists()
    pending = target.with_suffix(".pending")
    try:
        pipe(
            producer,
            (
                "openssl",
                "cms",
                "-encrypt",
                "-aes-256-gcm",
                "-binary",
                "-stream",
                "-outform",
                "DER",
                "-recip",
                str(cert),
                "-out",
                str(pending),
            ),
        )
        assert pending.is_file() and pending.stat().st_size > 0
        pending.chmod(0o400)
        pending.rename(target)
    finally:
        pending.unlink(missing_ok=True)
    return {"bytes": target.stat().st_size, "sha256": digest_file(target)}


def decrypt_command(target, cert, key):
    return (
        "openssl",
        "cms",
        "-decrypt",
        "-inform",
        "DER",
        "-binary",
        "-in",
        str(target),
        "-recip",
        str(cert),
        "-inkey",
        str(key),
    )


def verify_backup(target, cert, key, expected_sha):
    """Authenticate the whole ciphertext before any restore stream can write."""
    target = Path(target)
    assert target.is_file() and not target.is_symlink() and target.stat().st_nlink == 1
    assert stat.S_IMODE(target.stat().st_mode) == 0o400 and digest_file(target) == expected_sha
    with sp.Popen(decrypt_command(target, cert, key), stdout=sp.PIPE, stderr=sp.DEVNULL) as source:
        assert source.stdout is not None
        sha = hashlib.file_digest(source.stdout, "sha256").hexdigest()
        assert source.wait() == 0, "Backup authentication failed"
    return sha


def restore_backup(target, cert, key, expected_sha, consumer):
    """Write only after a full authentication pass; never emit plaintext logs."""
    plain_sha = verify_backup(target, cert, key, expected_sha)
    assert digest_file(target) == expected_sha
    pipe(decrypt_command(target, cert, key), consumer)
    return plain_sha


def restore_globals(target, cert, key, expected_sha, source_bootstrap, consumer):
    """Restore all grants, preserving the PG bootstrap identity used as grantor."""
    assert re.fullmatch(r"[a-z_][a-z0-9_]*", source_bootstrap)
    assert Path(target).stat().st_size <= 16 * 1024 * 1024
    plain_sha = verify_backup(target, cert, key, expected_sha)
    raw = checked(decrypt_command(target, cert, key))
    assert hashlib.sha256(raw).hexdigest() == plain_sha
    duplicate = f"CREATE ROLE {source_bootstrap};".encode()
    lines = raw.splitlines(keepends=True)
    assert sum(line.rstrip(b"\r\n") == duplicate for line in lines) == 1
    checked(
        consumer, input_bytes=b"".join(line for line in lines if line.rstrip(b"\r\n") != duplicate)
    )
    return plain_sha


def tar_manifest(producer):
    """Read archive paths/permissions/digests without extracting or printing contents."""
    entries = {}
    with sp.Popen(producer, stdout=sp.PIPE, stderr=sp.DEVNULL) as source:
        assert source.stdout is not None
        try:
            with tarfile.open(fileobj=source.stdout, mode="r|*") as archive:
                for entry in archive:
                    path = PurePosixPath(entry.name)
                    assert not path.is_absolute() and ".." not in path.parts
                    name = str(path)
                    assert name not in entries and (entry.isfile() or entry.isdir())
                    assert entry.uid >= 0 and entry.gid >= 0
                    sha = None
                    if entry.isfile():
                        file = archive.extractfile(entry)
                        assert file is not None
                        with file:
                            sha = hashlib.file_digest(file, "sha256").hexdigest()
                    entries[name] = (entry.uid, entry.gid, entry.mode, entry.size, sha)
            # The authenticated producer must reach and verify its final tag.
            while source.stdout.read(1024 * 1024):
                pass
        finally:
            source.stdout.close()
        assert source.wait() == 0
    return entries


def manifest_digest(entries, *, ownership=True):
    semantic = entries if ownership else {k: v[2:] for k, v in entries.items()}
    return hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


PG_SNAPSHOT = r"""
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL TIME ZONE 'UTC';
SELECT jsonb_build_object('kind','db','name',current_database(),
    'owner',pg_get_userbyid(datdba),'collate',datcollate,'ctype',datctype,'acl',datacl,
    'encoding',pg_encoding_to_char(encoding))
FROM pg_database WHERE datname=current_database();
SELECT jsonb_build_object('kind','identity','system_identifier',system_identifier::text)
FROM pg_control_system();
SELECT jsonb_build_object('kind','extension','name',extname,'version',extversion)
FROM pg_extension ORDER BY extname;
SELECT jsonb_build_object('kind','schema','name',nspname,
    'owner',pg_get_userbyid(nspowner),'acl',nspacl)
FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' ORDER BY nspname;
SELECT jsonb_build_object('kind','acl','name',n.nspname || '.' || c.relname,
   'owner',pg_get_userbyid(c.relowner),'acl',c.relacl,
   'columns',(SELECT coalesce(jsonb_object_agg(a.attname,a.attacl),'{}')
              FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0
              AND NOT a.attisdropped AND a.attacl IS NOT NULL))
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p','m','v','S','f') AND n.nspname !~ '^pg_'
AND n.nspname <> 'information_schema' ORDER BY n.nspname,c.relname;
SELECT jsonb_build_object('kind','routine_acl','name',n.nspname || '.' || p.proname || '(' ||
    pg_get_function_identity_arguments(p.oid) || ')',
    'owner',pg_get_userbyid(p.proowner),'acl',p.proacl)
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
ORDER BY n.nspname,p.proname,pg_get_function_identity_arguments(p.oid);
SELECT jsonb_build_object('kind','default_acl','name',
    jsonb_build_array(pg_get_userbyid(a.defaclrole),n.nspname,a.defaclobjtype)::text,'acl',a.defaclacl)
FROM pg_default_acl a LEFT JOIN pg_namespace n ON n.oid=a.defaclnamespace
ORDER BY pg_get_userbyid(a.defaclrole),n.nspname,a.defaclobjtype;
SELECT jsonb_build_object('kind','large_object_acl','name',oid::text,
    'owner',pg_get_userbyid(lomowner),'acl',lomacl)
FROM pg_largeobject_metadata ORDER BY oid;

SELECT jsonb_build_object('kind','globals','sha256',encode(sha256(convert_to((
    SELECT jsonb_build_array(
      (SELECT jsonb_agg(to_jsonb(r)-'oid' ORDER BY rolname) FROM pg_authid r),
      (SELECT jsonb_agg(jsonb_build_array(pr.rolname, pe.rolname, g.rolname, m.admin_option,
          m.inherit_option, m.set_option) ORDER BY pr.rolname,pe.rolname,g.rolname)
       FROM pg_auth_members m JOIN pg_authid pr ON pr.oid=m.roleid
       JOIN pg_authid pe ON pe.oid=m.member JOIN pg_authid g ON g.oid=m.grantor),
      (SELECT jsonb_agg(jsonb_build_array(d.datname,r.rolname,s.setconfig)
          ORDER BY d.datname,r.rolname)
       FROM pg_db_role_setting s LEFT JOIN pg_authid r ON r.oid=s.setrole
       LEFT JOIN pg_database d ON d.oid=s.setdatabase),
      (SELECT jsonb_agg(jsonb_build_array(parname,paracl) ORDER BY parname) FROM pg_parameter_acl)
    )
)::text,'UTF8')),'hex'));
SELECT format($q$
  SELECT jsonb_build_object('kind','table','name',%L,'count',count(*),'sha256',
    encode(sha256(convert_to(coalesce(string_agg(row_sha,'' ORDER BY row_sha),''),'UTF8')),'hex'))
  FROM (SELECT encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') row_sha
        FROM %I.%I t) rows
$q$, n.nspname || '.' || c.relname, n.nspname,c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p','m') AND n.nspname !~ '^pg_'
AND n.nspname <> 'information_schema'
ORDER BY n.nspname,c.relname
\gexec
SELECT format($q$
  SELECT jsonb_build_object('kind','sequence','name',%L,'last',last_value,'called',is_called)
  FROM %I.%I
$q$, n.nspname || '.' || c.relname,n.nspname,c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind='S' AND n.nspname !~ '^pg_'
ORDER BY n.nspname,c.relname
\gexec
SELECT jsonb_build_object('kind','large_objects','count',count(*),'sha256',
    encode(sha256(convert_to(coalesce(string_agg(row_sha,'' ORDER BY row_sha),''),'UTF8')),'hex'))
FROM (SELECT encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') row_sha
      FROM pg_largeobject t) rows;
COMMIT;
"""


def postgres_snapshot(command):
    """Capture only semantic digests and sequence metadata in one read-only snapshot."""
    rows = (
        checked(
            (
                *command,
                "-XqAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                "-",
            ),
            input_bytes=PG_SNAPSHOT.encode(),
        )
        .decode()
        .splitlines()
    )
    state = {}
    for row in rows:
        value = json.loads(row)
        key = (value["kind"], value.get("name", ""))
        assert key not in state
        state[key] = value
    assert {"db", "identity", "globals", "large_objects"} <= {key[0] for key in state}
    return state


def equal_restored_pg(source, restored):
    identity = ("identity", "")
    return source[identity] != restored[identity] and {
        key: val for key, val in source.items() if key != identity
    } == {key: val for key, val in restored.items() if key != identity}
