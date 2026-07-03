import * as fs from 'fs';
import * as path from 'path';
import { Verifier, SignatureMismatch, UnknownKey, PolicyViolation } from './index';

function runTests() {
    const vectorsDir = path.join(__dirname, '..', 'runtime', 'vectors');
    
    // Load trusted key
    const trustedKeyBytes = fs.readFileSync(path.join(vectorsDir, 'trusted_key.bin'));
    const trustedKeys = {
        'key1': new Uint8Array(trustedKeyBytes)
    };
    
    const verifier = new Verifier(trustedKeys);
    
    console.log("Running Verifier Tests...");

    // 1. valid.gfm
    try {
        const validBytes = fs.readFileSync(path.join(vectorsDir, 'valid.gfm'));
        const { header } = verifier.verify(new Uint8Array(validBytes));
        console.log(`✅ valid.gfm passed (Model: ${header.model_id}, Capabilities: ${header.capabilities})`);
    } catch (e: any) {
        console.error(`❌ valid.gfm failed unexpectedly: ${e.message}`);
        process.exit(1);
    }

    // 2. tampered.gfm
    try {
        const tamperedBytes = fs.readFileSync(path.join(vectorsDir, 'tampered.gfm'));
        verifier.verify(new Uint8Array(tamperedBytes));
        console.error(`❌ tampered.gfm should have failed!`);
        process.exit(1);
    } catch (e: any) {
        if (e instanceof SignatureMismatch) {
            console.log(`✅ tampered.gfm correctly rejected with SignatureMismatch`);
        } else {
            console.error(`❌ tampered.gfm failed with wrong error type: ${e.constructor.name}`);
            process.exit(1);
        }
    }

    // 3. expired-consent.gfm
    try {
        const expBytes = fs.readFileSync(path.join(vectorsDir, 'expired-consent.gfm'));
        verifier.verify(new Uint8Array(expBytes));
        console.error(`❌ expired-consent.gfm should have failed!`);
        process.exit(1);
    } catch (e: any) {
        if (e instanceof PolicyViolation) {
            console.log(`✅ expired-consent.gfm correctly rejected with PolicyViolation`);
        } else {
            console.error(`❌ expired-consent.gfm failed with wrong error type: ${e.constructor.name}`);
            process.exit(1);
        }
    }

    // 4. unknown-key.gfm
    try {
        const unkBytes = fs.readFileSync(path.join(vectorsDir, 'unknown-key.gfm'));
        verifier.verify(new Uint8Array(unkBytes));
        console.error(`❌ unknown-key.gfm should have failed!`);
        process.exit(1);
    } catch (e: any) {
        if (e instanceof UnknownKey) {
            console.log(`✅ unknown-key.gfm correctly rejected with UnknownKey`);
        } else {
            console.error(`❌ unknown-key.gfm failed with wrong error type: ${e.constructor.name}`);
            process.exit(1);
        }
    }

    // 5. valid-blob.gfm
    try {
        const blobBytes = fs.readFileSync(path.join(vectorsDir, 'valid-blob.gfm'));
        const { header, tensor } = verifier.verify(new Uint8Array(blobBytes));
        const blobStr = new TextDecoder().decode(tensor);
        console.log(`✅ valid-blob.gfm passed (Payload Type: ${header.payload_type}, Blob: ${blobStr})`);
    } catch (e: any) {
        console.error(`❌ valid-blob.gfm failed unexpectedly: ${e.message}`);
        process.exit(1);
    }

    // 6. tampered-blob.gfm
    try {
        const tamperedBlobBytes = fs.readFileSync(path.join(vectorsDir, 'tampered-blob.gfm'));
        verifier.verify(new Uint8Array(tamperedBlobBytes));
        console.error(`❌ tampered-blob.gfm should have failed!`);
        process.exit(1);
    } catch (e: any) {
        if (e instanceof SignatureMismatch) {
            console.log(`✅ tampered-blob.gfm correctly rejected with SignatureMismatch`);
        } else {
            console.error(`❌ tampered-blob.gfm failed with wrong error type: ${e.constructor.name}`);
            process.exit(1);
        }
    }
    
    console.log("All TS Verifier tests passed!");
}

runTests();
