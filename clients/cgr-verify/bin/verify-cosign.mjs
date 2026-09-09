#!/usr/bin/env node
// Conformance harness: read {record|subject, registry, ledger, trusted_issuers} as JSON on
// stdin, run the cgr.cosign.v1 verifier, print the result as JSON on stdout. Mirrors
// bin/verify-v4.mjs. `trusted_issuers` is the REQUIRED trusted issuer set (decision 0011 §8.2a).
import { verifyCosign } from '../src/cosign.js';

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (d) => { buf += d; });
process.stdin.on('end', async () => {
  try {
    const inp = JSON.parse(buf);
    const record = inp.record ?? inp.subject;
    const res = await verifyCosign(record, inp.registry || {}, inp.ledger || {}, inp.trusted_issuers);
    process.stdout.write(JSON.stringify(res));
  } catch (e) {
    process.stdout.write(JSON.stringify({ valid: false, reason: `harness error: ${e && e.message ? e.message : e}` }));
    process.exitCode = 0;
  }
});
