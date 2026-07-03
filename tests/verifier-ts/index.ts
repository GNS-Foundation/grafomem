import * as crypto from 'crypto';

export class SignatureMismatch extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'SignatureMismatch';
    }
}

export class UnknownKey extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'UnknownKey';
    }
}

export class PolicyViolation extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'PolicyViolation';
    }
}

export interface Consent {
    subject_id: string;
    policy: string;
    expires_at?: string;
}

export interface CSOHeader {
    model_id: string;
    capabilities: string[];
    consent: Consent;
    sig_alg: string;
    key_id: string;
    payload_type?: string;
}

export class Verifier {
    constructor(private trustedKeys: Record<string, Uint8Array>) {}

    verify(gfmBytes: Uint8Array): { header: CSOHeader, tensor: Uint8Array } {
        if (gfmBytes.length < 4 || new TextDecoder().decode(gfmBytes.slice(0, 4)) !== "GFM1") {
            throw new Error("Invalid GFM magic");
        }
        let offset = 4;
        
        // Read header length (uint32 little endian)
        const dv = new DataView(gfmBytes.buffer, gfmBytes.byteOffset, gfmBytes.byteLength);
        const headerLen = dv.getUint32(offset, true);
        offset += 4;
        
        // Read header
        const headerBytes = gfmBytes.slice(offset, offset + headerLen);
        const headerStr = new TextDecoder().decode(headerBytes);
        const header: CSOHeader = JSON.parse(headerStr);
        offset += headerLen;
        
        // Read tensor length (uint32 little endian)
        const tensorLen = dv.getUint32(offset, true);
        offset += 4;
        
        // Read tensor
        const tensorBytes = gfmBytes.slice(offset, offset + tensorLen);
        offset += tensorLen;
        
        // Read signature
        const signature = gfmBytes.slice(offset, offset + 64);
        
        // Verify signature
        if (header.sig_alg !== "ed25519") {
            throw new Error(`Unsupported signature algorithm: ${header.sig_alg}`);
        }
        
        const keyBytes = this.trustedKeys[header.key_id];
        if (!keyBytes) {
            throw new UnknownKey(`unknown key_id: ${header.key_id}`);
        }

        // We construct a key object from the raw bytes (Node.js >= 16 supports format 'raw')
        // Convert raw 32-byte Ed25519 public key to DER SPKI format
        const spkiPrefix = Buffer.from('302a300506032b6570032100', 'hex');
        const derKey = Buffer.concat([spkiPrefix, Buffer.from(keyBytes)]);
        
        const publicKey = crypto.createPublicKey({
            key: derKey,
            format: 'der',
            type: 'spki'
        });
        
        // The payload that was signed is (headerBytes + tensorBytes)
        const payload = Buffer.concat([Buffer.from(headerBytes), Buffer.from(tensorBytes)]);
        
        const isValid = crypto.verify(
            undefined, // algorithm is implicitly undefined for Ed25519 in crypto.verify
            payload,
            publicKey,
            Buffer.from(signature)
        );
        
        if (!isValid) {
            throw new SignatureMismatch("signature mismatch");
        }
        
        // Check consent expiration
        if (header.consent && header.consent.expires_at) {
            const exp = new Date(header.consent.expires_at);
            if (exp.getTime() < Date.now()) {
                throw new PolicyViolation("Consent expired");
            }
        }
        
        return { header, tensor: tensorBytes };
    }
}
