"""
GRAFOMEM CLI — the product surface.

    grafomem conformance  --backend MODULE:CLASS
    grafomem run          --workload W1 W2 ... --backend MODULE:CLASS
    grafomem corpus       info | verify
    grafomem report       --input results.json --format json|markdown

Every command resolves MODULE:CLASS to a MemoryBackend instance via dynamic
import. The conformance command is the primary customer-facing entry point:
it runs the GMP §8 suite and emits a structured compliance report.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Backend loader — "my.module:MyBackend" → callable factory
# ---------------------------------------------------------------------------

def _load_backend_class(spec: str) -> type:
    """Import MODULE:CLASS and return the class object."""
    if ":" not in spec:
        raise click.BadParameter(
            f"Backend must be MODULE:CLASS (got {spec!r}). "
            f"Example: aml.backends.gmp_reference:GMPReferenceBackend"
        )
    module_path, class_name = spec.rsplit(":", 1)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        raise click.BadParameter(f"Cannot import module {module_path!r}: {e}") from e
    try:
        cls = getattr(module, class_name)
    except AttributeError as e:
        raise click.BadParameter(
            f"Module {module_path!r} has no class {class_name!r}"
        ) from e
    return cls


def _make_factory(spec: str, embed: str | None = None):
    """Return a zero-arg factory that creates fresh backend instances.

    For backends that need an embed_fn (VectorOnlyBackend, GMPReferenceBackend),
    the --embedder flag selects stub vs BGE. Backends whose __init__ doesn't
    accept embed_fn are instantiated bare.
    """
    cls = _load_backend_class(spec)

    # Inspect whether the class needs an embed_fn argument.
    import inspect
    sig = inspect.signature(cls.__init__)
    needs_embed = "embed_fn" in sig.parameters

    if needs_embed:
        embed_fn = _resolve_embedder(embed or "stub")
        return lambda: cls(embed_fn=embed_fn)
    return lambda: cls()


def _resolve_embedder(name: str):
    """Return the embedder function for --embedder flag."""
    if name == "stub":
        from aml.backends.vector_only import _stub_embedder
        return _stub_embedder()
    elif name == "bge":
        from aml.backends.vector_only import _default_embedder
        return _default_embedder()
    else:
        raise click.BadParameter(f"Unknown embedder: {name!r}. Use 'stub' or 'bge'.")


# ============================================================================
# CLI root
# ============================================================================

@click.group()
@click.version_option(package_name="grafomem")
def main():
    """GRAFOMEM — agent-memory conformance benchmark and compliance toolkit."""
    pass


def _read_corpus_hash() -> str:
    """Read corpus hash from corpus.lock, or return 'unknown'."""
    lock_path = Path("corpus/corpus.lock")
    if lock_path.exists():
        try:
            lock = json.loads(lock_path.read_text())
            return lock.get("corpus_hash", "unknown")
        except Exception:
            pass
    return "unknown"


# ============================================================================
# grafomem check
# ============================================================================

@main.command()
@click.option("--backend", "-b", required=True,
              help="Backend class as MODULE:CLASS")
def check(backend):
    """Quick pre-flight validation of a backend adapter.

    Checks Protocol compliance, method signatures, and basic round-trip
    BEFORE running the full conformance suite. Fails fast with actionable errors.
    """
    from aml.adapter_check import print_check
    cls = _load_backend_class(backend)
    ok = print_check(cls)
    if not ok:
        sys.exit(1)


# ============================================================================
# grafomem init
# ============================================================================

@main.command()
@click.argument("directory", default=".")
def init(directory):
    """Scaffold a new GMP backend adapter project.

    Copies the adapter template (my_backend.py, conftest.py, pyproject.toml,
    README.md) into DIRECTORY. Existing files are not overwritten.

    \b
    Examples:
        grafomem init                  # scaffold in current dir
        grafomem init my-adapter/      # scaffold in my-adapter/
    """
    import shutil

    # Locate the adapter_template/ bundled with grafomem
    template_dir = Path(__file__).resolve().parent.parent.parent / "adapter_template"
    if not template_dir.is_dir():
        click.echo(f"✗ Template directory not found at {template_dir}", err=True)
        sys.exit(1)

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)

    copied, skipped = 0, 0
    for src in sorted(template_dir.iterdir()):
        if src.name.startswith(".") or src.name == "__pycache__":
            continue
        dst = target / src.name
        if dst.exists():
            click.echo(f"  ⚠ {src.name} — already exists, skipping")
            skipped += 1
        else:
            shutil.copy2(src, dst)
            click.echo(f"  ✓ {src.name}")
            copied += 1

    click.echo(f"\n✓ Scaffolded {copied} file(s) into {target.resolve()}"
               + (f" ({skipped} skipped)" if skipped else ""))
    click.echo("\nNext steps:")
    click.echo("  1. Edit my_backend.py — wire in your storage system")
    click.echo("  2. grafomem check -b my_backend:MyBackend")
    click.echo("  3. grafomem conformance -b my_backend:MyBackend -o report.json")


# ============================================================================
# grafomem conformance
# ============================================================================

@main.command()
@click.option("--backend", "-b", default=None,
              help="Backend class as MODULE:CLASS (e.g. aml.backends.gmp_reference:GMPReferenceBackend)")
@click.option("--url", "-u", default=None,
              help="Audit a live server URL (e.g. https://grafomem-production.up.railway.app)")
@click.option("--token", "-t", default=None,
              help="Bearer token for authenticated servers (used with --url)")
@click.option("--embedder", "-e", default="stub", type=click.Choice(["stub", "bge"]),
              help="Embedding function to use (default: stub)")
@click.option("--seeds", "-s", default=5, type=int, help="Number of seeds (default: 5)")
@click.option("--budget", default=512, type=int, help="Token budget for retrieval (default: 512)")
@click.option("--strict", is_flag=True, help="Raise on any conformance violation")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Write report to file (default: stdout summary)")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "markdown"]),
              help="Report format when --output is used (default: json)")
@click.option("--sign-key", type=click.Path(exists=True), default=None,
              help="Ed25519 private key file (32 raw bytes) to sign the report")
def conformance(backend, url, token, embedder, seeds, budget, strict, output, fmt, sign_key):
    """Run the GMP conformance suite against a backend or live server.

    Tests only declared capabilities — honest omission is never penalized.
    Emits per-capability PASS/FAIL with two-sided directional metrics and
    the M8 conformance rate.

    \b
    Local backend:
        grafomem conformance -b aml.backends.gmp_reference:GMPReferenceBackend

    \b
    Live server audit:
        grafomem conformance --url https://grafomem-production.up.railway.app
        grafomem conformance --url https://my-server.com --token gfm_abc123

    \b
    Signed certificate:
        grafomem conformance --url https://my-server.com -o report.json --sign-key key.bin
    """
    if not backend and not url:
        raise click.UsageError("Provide either --backend/-b or --url/-u")
    if backend and url:
        raise click.UsageError("Provide --backend or --url, not both")

    from aml.eval.conformance import run_conformance, print_profile
    from aml.eval.report import from_profile, to_json, to_markdown

    # Build the factory: local class or remote GMPClient
    if url:
        from aml.wire import GMPClient
        name = url.rstrip("/").split("//")[-1]  # e.g. "grafomem-production.up.railway.app"
        factory = lambda: GMPClient.create(url, auth_token=token)
        click.echo(f"GRAFOMEM remote conformance audit")
        click.echo(f"  Target:  {url}")
        click.echo(f"  Auth:    {'Bearer token' if token else 'none'}")
        click.echo(f"  Seeds:   {seeds}")
        click.echo(f"  Budget:  {budget}")
        click.echo()
    else:
        factory = _make_factory(backend, embedder)
        name = backend.rsplit(":", 1)[-1]
        click.echo(f"GRAFOMEM conformance suite — {name} (seeds={seeds}, budget={budget})\n")

    t0 = time.perf_counter()

    profile = run_conformance(
        factory, name=name, seeds=range(seeds), budget=budget, strict=strict,
    )

    elapsed = time.perf_counter() - t0
    print_profile(profile)
    click.echo(f"\nM8 conformance rate: {profile.conformance_rate:.3f}")
    click.echo(f"Elapsed: {elapsed:.1f}s")

    if output:
        corpus_hash = _read_corpus_hash()
        report = from_profile(profile, corpus_hash=corpus_hash)

        if sign_key:
            from aml.eval.report import sign_report
            key_bytes = Path(sign_key).read_bytes()
            report = sign_report(report, key_bytes)
            click.echo(f"\n🔐 Report signed (Ed25519, pubkey={report.signed_by[:16]}...)")

        content = to_markdown(report) if fmt == "markdown" else to_json(report)
        Path(output).write_text(content)
        click.echo(f"Report ({fmt}) written to {output}")



def _profile_to_dict(profile) -> dict:
    """Serialize a ConformanceProfile to a JSON-friendly dict."""
    return {
        "store": profile.store,
        "declared": sorted(c.value for c in profile.declared),
        "supported": sorted(c.value for c in profile.supported),
        "m8_conformance_rate": profile.conformance_rate,
        "violations": [
            {
                "capability": r.capability.value,
                "workload": r.workload,
                "directions": [
                    {
                        "name": d.name,
                        "objective": d.objective,
                        "point": d.point,
                        "ci": list(d.ci),
                        "passed": d.passed,
                    }
                    for d in r.directions
                ],
            }
            for r in profile.violations
        ],
        "results": [
            {
                "capability": r.capability.value,
                "workload": r.workload,
                "passed": r.passed,
                "directions": [
                    {
                        "name": d.name,
                        "objective": d.objective,
                        "point": d.point,
                        "ci": list(d.ci),
                        "passed": d.passed,
                    }
                    for d in r.directions
                ],
            }
            for r in profile.results
        ],
    }


# ============================================================================
# grafomem run
# ============================================================================

@main.command()
@click.option("--backend", "-b", required=True,
              help="Backend class as MODULE:CLASS")
@click.option("--embedder", "-e", default="stub", type=click.Choice(["stub", "bge"]),
              help="Embedding function (default: stub)")
@click.option("--workload", "-w", required=True, multiple=True,
              help="Workload(s) to run (e.g. W1 W2 W5)")
@click.option("--seeds", "-s", default=5, type=int, help="Number of seeds")
@click.option("--difficulty", "-d", default="hard",
              type=click.Choice(["easy", "medium", "hard"]),
              help="Trace difficulty (default: hard)")
@click.option("--budget", default=512, type=int, help="Token budget for retrieval")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Write JSON results to file")
def run(backend, embedder, workload, seeds, difficulty, budget, output):
    """Run workloads against a backend and compute M1–M3 metrics.

    Example: grafomem run -b aml.backends.vector_only:VectorOnlyBackend -w W1 W2
    """
    from aml.generator.trace import Difficulty
    from aml.eval.harness import run_trace
    from aml.eval.metrics import score_run

    diff = Difficulty(difficulty)
    factory = _make_factory(backend, embedder)
    name = backend.rsplit(":", 1)[-1]

    # Map workload names to generators
    generators = _get_generators(workload)

    all_results = {}
    for wname, gen_fn in generators.items():
        click.echo(f"\n{'='*60}")
        click.echo(f"  {wname} — {name} — {difficulty} — {seeds} seeds")
        click.echo(f"{'='*60}")

        seed_scores = []
        for s in range(seeds):
            tr = gen_fn(seed=s, difficulty=diff)
            store = factory()
            result = run_trace(store, tr, budget_tokens=budget)
            scores = score_run(result, tr)
            seed_scores.append(scores)
            click.echo(f"  seed {s}: M1={scores['m1']:.3f}  M2={scores['m2']:.3f}  "
                        f"M3={scores['m3']:.1f}")

        # Aggregate across seeds
        from statistics import mean, stdev
        agg = {}
        for metric in ["m1", "m2", "m3"]:
            vals = [s[metric] for s in seed_scores]
            finite = [v for v in vals if v != float("inf")]
            if finite:
                mu = mean(finite)
                sd = stdev(finite) if len(finite) > 1 else 0.0
                agg[metric] = {"mean": mu, "std": sd, "values": vals}
            else:
                agg[metric] = {"mean": float("inf"), "std": 0.0, "values": vals}

        # M4 latency: merge all seeds' latency data
        from collections import defaultdict
        all_lats = defaultdict(list)
        for s in seed_scores:
            m4 = s.get("m4", {})
            for op, stats in m4.items():
                all_lats[op].append(stats)
        if all_lats:
            agg["m4"] = {
                op: {
                    "p50_mean": mean(d["p50"] for d in dlist),
                    "p95_mean": mean(d["p95"] for d in dlist),
                    "p99_mean": mean(d["p99"] for d in dlist),
                }
                for op, dlist in all_lats.items()
            }

        click.echo(f"\n  Aggregate ({seeds} seeds):")
        click.echo(f"    M1 = {agg['m1']['mean']:.3f} ± {agg['m1']['std']:.3f}")
        click.echo(f"    M2 = {agg['m2']['mean']:.3f} ± {agg['m2']['std']:.3f}")
        m3_val = agg["m3"]["mean"]
        m3_str = f"{m3_val:.1f}" if m3_val != float("inf") else "inf"
        click.echo(f"    M3 = {m3_str} ± {agg['m3']['std']:.1f}")
        if "m4" in agg:
            for op in ("write", "retrieve", "supersede", "delete"):
                if op in agg["m4"]:
                    d = agg["m4"][op]
                    click.echo(f"    M4.{op:10s}  P50={d['p50_mean']:.2f}ms  "
                               f"P95={d['p95_mean']:.2f}ms  P99={d['p99_mean']:.2f}ms")
        all_results[wname] = agg

    if output:
        Path(output).write_text(json.dumps(all_results, indent=2, default=str))
        click.echo(f"\nResults written to {output}")


def _get_generators(workload_names: tuple[str, ...]) -> dict:
    """Map workload name strings to generator functions."""
    mapping = {}
    for w in workload_names:
        w_upper = w.upper()
        if w_upper == "W1":
            from aml.generator.workloads.w1 import generate_w1
            mapping["W1"] = generate_w1
        elif w_upper == "W2":
            from aml.generator.workloads.w2 import generate_w2
            mapping["W2"] = generate_w2
        elif w_upper == "W3":
            from aml.generator.workloads.w3 import generate_w3
            mapping["W3"] = generate_w3
        elif w_upper == "W4":
            from aml.generator.workloads.w4 import generate_w4
            mapping["W4"] = generate_w4
        elif w_upper == "W5":
            from aml.generator.workloads.w5 import generate_w5
            mapping["W5"] = generate_w5
        elif w_upper == "W6":
            from aml.generator.workloads.w6 import generate_w6
            mapping["W6"] = generate_w6
        elif w_upper == "W7":
            from aml.generator.workloads.w7 import generate_w7
            mapping["W7"] = generate_w7
        elif w_upper == "W9":
            from aml.generator.workloads.w9 import generate_w9
            mapping["W9"] = generate_w9
        else:
            raise click.BadParameter(f"Unknown workload: {w!r}. "
                                      f"Valid: W1–W7, W9 (W8 held out, W10 via conformance)")
    return mapping


# ============================================================================
# grafomem corpus
# ============================================================================

@main.group()
def corpus():
    """Corpus management — info, verify, generate."""
    pass


@corpus.command()
def info():
    """Show corpus metadata from corpus.lock."""
    lock_path = Path("corpus/corpus.lock")
    if not lock_path.exists():
        click.echo("No corpus.lock found in corpus/. Run `grafomem corpus generate` first.")
        sys.exit(1)

    lock = json.loads(lock_path.read_text())
    click.echo(f"Corpus:       {lock['name']}")
    click.echo(f"Schema:       {lock['schema_version']}")
    click.echo(f"Generator:    {lock['generator_version']}")
    click.echo(f"Generated:    {lock['generated_at']}")
    click.echo(f"Traces:       {lock['n_traces']}")
    click.echo(f"Corpus hash:  {lock['corpus_hash']}")
    click.echo(f"\nWorkload rollup hashes:")
    for w, h in sorted(lock["workload_hashes"].items()):
        n_traces = sum(1 for k in lock["trace_hashes"] if k.startswith(w + "_"))
        click.echo(f"  {w:6s}  {h[:16]}...  ({n_traces} traces)")


@corpus.command()
def verify():
    """Verify corpus integrity — re-hash traces and check against lock."""
    lock_path = Path("corpus/corpus.lock")
    if not lock_path.exists():
        click.echo("No corpus.lock found.")
        sys.exit(1)

    import hashlib

    lock = json.loads(lock_path.read_text())
    traces_dir = Path("corpus/traces")
    errors = []
    ok = 0

    for trace_name, expected_hash in sorted(lock["trace_hashes"].items()):
        trace_file = traces_dir / f"{trace_name}.jsonl"
        if not trace_file.exists():
            errors.append(f"MISSING: {trace_file}")
            continue
        # Match generate_corpus.py's content_hash: parse JSON, strip
        # non-deterministic fields, re-canonicalize, then hash.
        trace_dict = json.loads(trace_file.read_text(encoding="utf-8"))
        trace_dict.pop("trace_id", None)
        trace_dict.pop("generated_at", None)
        canonical = json.dumps(trace_dict, sort_keys=True, separators=(",", ":"))
        actual = hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).hexdigest()
        if actual != expected_hash:
            errors.append(f"MISMATCH: {trace_name} expected={expected_hash[:16]}... actual={actual[:16]}...")
        else:
            ok += 1

    if errors:
        click.echo(f"\n✓ {ok} traces OK")
        click.echo(f"✗ {len(errors)} error(s):")
        for e in errors:
            click.echo(f"  {e}")
        sys.exit(1)
    else:
        click.echo(f"\n✓ All {len(lock['trace_hashes'])} traces verified against corpus.lock")


# ============================================================================
# grafomem serve
# ============================================================================

@main.command()
@click.option("--host", default="0.0.0.0", help="Bind address")
@click.option("--port", "-p", default=8642, type=int, help="Port")
@click.option("--backend", "-b",
              default="aml.backends.sqlite_gmp:SQLiteGMPBackend",
              help="Backend class (MODULE:CLASS) or shortcut: 'sqlite', 'postgres'")
@click.option("--db", default=None,
              help="Database path (SQLite) or connection URL (PostgreSQL)")
@click.option("--embedder", "-e", type=click.Choice(["stub", "bge"]),
              default="bge", help="Embedder for vector backends")
@click.option("--mcp", type=click.Choice(["none", "stdio", "sse"]),
              default="none", help="MCP transport mode")
@click.option("--mcp-port", default=8643, type=int, help="Port for MCP SSE transport")
@click.option("--auth", type=click.Choice(["none", "token"]),
              default="none", help="Authentication mode")
@click.option("--batch/--no-batch", default=False,
              help="Enable batched ingestion for writes")
@click.option("--batch-size", default=64, type=int,
              help="Batch size for ingestion queue")
def serve(host, port, backend, db, embedder, mcp, mcp_port, auth, batch, batch_size):
    """Start the GRAFOMEM memory server.

    \b
    Examples:
        grafomem serve                                    # SQLite + BGE
        grafomem serve -b postgres --db postgresql://...   # PostgreSQL + pgvector
        grafomem serve --mcp stdio                        # MCP over stdin/stdout
        grafomem serve --auth token --batch               # multi-tenant + batched
    """
    import inspect
    import os

    # Resolve backend shortcuts
    _SHORTCUTS = {
        "sqlite": "aml.backends.sqlite_gmp:SQLiteGMPBackend",
        "postgres": "aml.backends.postgres_gmp:PostgresGMPBackend",
        "postgresql": "aml.backends.postgres_gmp:PostgresGMPBackend",
        "reference": "aml.backends.gmp_reference:GMPReferenceBackend",
    }
    backend_spec = _SHORTCUTS.get(backend, backend)
    is_postgres = "postgres_gmp" in backend_spec

    # Default db values
    if db is None:
        if is_postgres:
            db = os.environ.get(
                "GRAFOMEM_DB_URL",
                "postgresql://grafomem:grafomem@localhost:5432/grafomem"
            )
        else:
            db = os.environ.get("GRAFOMEM_DB_PATH", "grafomem.db")

    cls = _load_backend_class(backend_spec)
    sig = inspect.signature(cls.__init__)

    def _factory():
        kwargs = {}
        if "embed_fn" in sig.parameters:
            if embedder == "bge":
                from aml.backends.vector_only import _default_embedder
                kwargs["embed_fn"] = _default_embedder()
            else:
                from aml.backends.vector_only import _stub_embedder
                _batch_fn = _stub_embedder()
                def _single_str_stub(text):
                    import numpy as np
                    if isinstance(text, str):
                        return _batch_fn([text])[0]
                    return _batch_fn(text)
                kwargs["embed_fn"] = _single_str_stub
        # Wire the database connection
        if "db_url" in sig.parameters:
            kwargs["db_url"] = db
        elif "db_path" in sig.parameters:
            kwargs["db_path"] = db
        # Wire signing identity if accepted
        if "signing_identity" in sig.parameters:
            from aml.cloud.identity import EnvIdentity
            kwargs["signing_identity"] = EnvIdentity() if os.environ.get("GRAFOMEM_SIGNING_KEY") else None
        return cls(**kwargs)

    # MCP mode: run the MCP server directly (no FastAPI)
    if mcp in ("stdio", "sse"):
        import asyncio
        click.echo(f"GRAFOMEM MCP server (transport={mcp})")

        if mcp == "stdio":
            from aml.server.mcp import run_mcp_stdio
            asyncio.run(run_mcp_stdio(_factory))
        else:
            from aml.server.mcp import run_mcp_sse
            asyncio.run(run_mcp_sse(_factory, host=host, port=mcp_port))
        return

    # HTTP mode: FastAPI server
    try:
        import uvicorn
        from aml.server.app import create_app
    except ImportError as e:
        click.echo(
            f"✗ Server dependencies not installed: {e}\n"
            f"  Install with: pip install grafomem[server]",
            err=True,
        )
        sys.exit(1)

    app = create_app(
        backend_factory=_factory,
        auth_mode=auth,
        enable_batching=batch,
        batch_size=batch_size,
        db_url=db if is_postgres else None,
    )

    db_display = db if not is_postgres else db.split("@")[-1] if "@" in db else db
    engine_name = "PostgreSQL + pgvector" if is_postgres else "SQLite + sqlite-vec"

    click.echo(f"GRAFOMEM server v0.2.0")
    click.echo(f"  Engine:    {engine_name}")
    click.echo(f"  Backend:   {backend_spec}")
    click.echo(f"  Database:  {db_display}")
    click.echo(f"  Embedder:  {embedder}")
    click.echo(f"  Auth:      {auth}")
    click.echo(f"  Batching:  {'ON (batch_size=' + str(batch_size) + ')' if batch else 'OFF'}")
    click.echo(f"  Cloud:     {'ON (/v1/cloud)' if is_postgres else 'OFF'}")
    click.echo(f"  Listening: http://{host}:{port}")
    click.echo(f"  Docs:      http://{host}:{port}/docs")
    click.echo()

    uvicorn.run(app, host=host, port=port, log_level="info")


# ============================================================================
# Smoke check
# ============================================================================

if __name__ == "__main__":
    main()
