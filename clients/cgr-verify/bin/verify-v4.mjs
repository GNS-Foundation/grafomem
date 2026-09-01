#!/usr/bin/env node
// Conformance harness: read {subject, ledger, pinned_issuer, held_edges} as JSON on stdin,
// run the v4 verifier, print the VerifyResult as JSON on stdout. Used by the Python bridge.
import { verifyCGRAttestationV4 } from '../src/index.js';

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (d) => { buf += d; });
process.stdin.on('end', async () => {
  try {
    const { subject, ledger, pinned_issuer, held_edges } = JSON.parse(buf);
    const res = await verifyCGRAttestationV4(subject, ledger || {}, pinned_issuer, held_edges || []);
    process.stdout.write(JSON.stringify(res));
  } catch (e) {
    process.stdout.write(JSON.stringify({ valid: false, reason: `harness error: ${e && e.message ? e.message : e}` }));
    process.exitCode = 0; // report via JSON, not exit code
  }
});
