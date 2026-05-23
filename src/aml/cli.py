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
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise click.UsageError(
                "BGE embedder requires sentence-transformers. "
                "Install with: pip install grafomem[backends]"
            )
        model = SentenceTransformer("BAAI/bge-small-en-v1.5")
        return lambda texts: model.encode(texts, normalize_embeddings=True).tolist()
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


# ============================================================================
# grafomem conformance
# ============================================================================

@main.command()
@click.option("--backend", "-b", required=True,
              help="Backend class as MODULE:CLASS (e.g. aml.backends.gmp_reference:GMPReferenceBackend)")
@click.option("--embedder", "-e", default="stub", type=click.Choice(["stub", "bge"]),
              help="Embedding function to use (default: stub)")
@click.option("--seeds", "-s", default=5, type=int, help="Number of seeds (default: 5)")
@click.option("--budget", default=512, type=int, help="Token budget for retrieval (default: 512)")
@click.option("--strict", is_flag=True, help="Raise on any conformance violation")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Write JSON report to file (default: stdout summary)")
def conformance(backend, embedder, seeds, budget, strict, output):
    """Run the GMP conformance suite against a backend.

    Tests only declared capabilities — honest omission is never penalized.
    Emits per-capability PASS/FAIL with two-sided directional metrics and
    the M8 conformance rate.
    """
    from aml.eval.conformance import run_conformance, print_profile

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
        report = _profile_to_dict(profile)
        Path(output).write_text(json.dumps(report, indent=2, default=str))
        click.echo(f"\nReport written to {output}")


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

        click.echo(f"\n  Aggregate ({seeds} seeds):")
        click.echo(f"    M1 = {agg['m1']['mean']:.3f} ± {agg['m1']['std']:.3f}")
        click.echo(f"    M2 = {agg['m2']['mean']:.3f} ± {agg['m2']['std']:.3f}")
        m3_val = agg["m3"]["mean"]
        m3_str = f"{m3_val:.1f}" if m3_val != float("inf") else "inf"
        click.echo(f"    M3 = {m3_str} ± {agg['m3']['std']:.1f}")
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
# Smoke check
# ============================================================================

if __name__ == "__main__":
    main()
