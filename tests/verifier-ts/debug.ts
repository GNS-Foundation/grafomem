import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';

const gfmBytes = fs.readFileSync('../runtime/vectors/valid.gfm');
const trustedKeyBytes = fs.readFileSync('../runtime/vectors/trusted_key.bin');

let offset = 4;
const dv = new DataView(gfmBytes.buffer, gfmBytes.byteOffset, gfmBytes.byteLength);
const headerLen = dv.getUint32(offset, true);
offset += 4;
const headerBytes = gfmBytes.slice(offset, offset + headerLen);
offset += headerLen;
const tensorLen = Number(dv.getBigUint64(offset, true));
offset += 8;
const tensorBytes = gfmBytes.slice(offset, offset + tensorLen);
offset += tensorLen;
const signature = gfmBytes.slice(offset, offset + 64);

const payload = Buffer.concat([Buffer.from(headerBytes), Buffer.from(tensorBytes)]);
console.log("header len", headerBytes.length);
console.log("tensor len", tensorBytes.length);
console.log("sig len", signature.length);
console.log("payload len", payload.length);
console.log("trusted key len", trustedKeyBytes.length);

const spkiPrefix = Buffer.from('302a300506032b6570032100', 'hex');
const derKey = Buffer.concat([spkiPrefix, Buffer.from(trustedKeyBytes)]);
const publicKey = crypto.createPublicKey({ key: derKey, format: 'der', type: 'spki' });

const isValid = crypto.verify(undefined, payload, publicKey, Buffer.from(signature));
console.log("isValid", isValid);

