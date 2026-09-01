#!/usr/bin/env node
// Conformance harness: read {subject, ledger, pinned_issuer, held_edges, mode, seek_fails}
// as JSON on stdin, run the v4 verifier, print the VerifyResult as JSON on stdout.
//
// The `seek` capability (enforcing mode) is implemented HERE as the trivial in-memory scan of
// the provided ledger — the same interface a real consumer (@geiant/core, read surface)
// implements against its own store/index. `seek_fails` simulates a query error/timeout.
import { verifyCGRAttestationV4, attestationFingerprint } from '../src/index.js';

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (d) => { buf += d; });
process.stdin.on('end', async () => {
  try {
    const { subject, ledger, pinned_issuer, held_edges, mode, seek_fails } = JSON.parse(buf);

    // seek(subjectFp) → Foundation-signed edge-records in the ledger whose relates_to targets it.
    const seek = async (subjFp) => {
      if (seek_fails) throw new Error('simulated seek failure');
      const atts = (ledger && ledger.attestations) || {};
      const hits = [];
      for (const rec of Object.values(atts)) {
        for (const e of (Array.isArray(rec.relates_to) ? rec.relates_to : [])) {
          if (e.target && e.target.kind === 'attestation' && e.target.hash === subjFp
              && (e.type === 'revokes' || e.type === 'supersedes')) {
            hits.push(rec);
            break;
          }
        }
      }
      return hits;
    };

    const res = await verifyCGRAttestationV4(subject, ledger || {}, pinned_issuer, {
      mode, heldEdges: held_edges || [], seek,
    });
    process.stdout.write(JSON.stringify(res));
  } catch (e) {
    process.stdout.write(JSON.stringify({ valid: false, reason: `harness error: ${e && e.message ? e.message : e}` }));
    process.exitCode = 0;
  }
});
