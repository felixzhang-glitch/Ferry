// Run with: node --test tests/
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

// Caps outbound at 1 MB so the oversize guard is testable without huge fixtures.
// Must be set before the dynamic import, which reads it at module scope.
process.env.WECHAT_MAX_FILE_MB = "1";

const sidecar = await import("../lib/js/wechat-sidecar.mjs");

const {
  aesEcbPaddedSize,
  buildFileItem,
  collectFileItems,
  collectImageItems,
  decryptMediaBuffer,
  describeFileItem,
  encryptAesEcb,
  extractFilePayload,
  resolveSendableFile,
  sanitizeFileName,
} = sidecar;

test("aesEcbPaddedSize adds a full block on exact 16-byte multiples", () => {
  const cases = [
    [0, 16],
    [1, 16],
    [15, 16],
    [16, 32],
    [17, 32],
    [31, 32],
    [32, 48],
    [2235164, 2235168],
  ];
  for (const [input, expected] of cases) {
    assert.equal(aesEcbPaddedSize(input), expected, `paddedSize(${input})`);
  }
});

test("encrypt output length matches the padded size", () => {
  const key = crypto.randomBytes(16);
  for (const size of [1, 15, 16, 17, 4096]) {
    const plaintext = crypto.randomBytes(size);
    assert.equal(encryptAesEcb(plaintext, key).length, aesEcbPaddedSize(size), `ciphertext len for ${size}`);
  }
});

test("buildFileItem aes_key round-trips through the inbound decoder", () => {
  const key = crypto.randomBytes(16);
  const plaintext = Buffer.from("简历 resume payload 中文测试".repeat(4), "utf8");
  const item = buildFileItem({
    encryptQueryParam: "EQP-abc",
    aeskeyHex: key.toString("hex"),
    fileName: "resume.pdf",
    rawsize: plaintext.length,
  });

  // The inbound path receives aes_key exactly as built here, so this proves the
  // base64-of-hex encoding is symmetric rather than base64 of raw bytes.
  const recovered = decryptMediaBuffer(encryptAesEcb(plaintext, key), item.file_item.media.aes_key, plaintext.length);
  assert.deepEqual(recovered, plaintext);
});

test("decryptMediaBuffer decodes a plain 32-char hex key (inbound image aeskey)", () => {
  const key = crypto.randomBytes(16);
  const plaintext = crypto.randomBytes(2048);
  // Inbound image items carry image_item.aeskey as a plain 32-char hex string,
  // not base64-of-hex like files. Before the fix this threw
  // "unexpected aes key length: 32" and every inbound image failed to archive.
  const recovered = decryptMediaBuffer(encryptAesEcb(plaintext, key), key.toString("hex"), plaintext.length);
  assert.deepEqual(recovered, plaintext);
});

test("buildFileItem emits the protocol shape iLink expects", () => {
  const keyHex = crypto.randomBytes(16).toString("hex");
  const item = buildFileItem({ encryptQueryParam: "EQP", aeskeyHex: keyHex, fileName: "a.pdf", rawsize: 2235164 });

  // MessageItemType.FILE is 4, distinct from UploadMediaType.FILE which is 3.
  assert.equal(item.type, 4);
  assert.equal(item.file_item.media.encrypt_query_param, "EQP");
  assert.equal(item.file_item.media.encrypt_type, 1);
  assert.equal(item.file_item.file_name, "a.pdf");
  // len is the plaintext byte count, stringified.
  assert.equal(item.file_item.len, "2235164");
  assert.equal(typeof item.file_item.len, "string");
  // aes_key is base64 of the hex string, so it decodes back to 32 hex chars.
  const decoded = Buffer.from(item.file_item.media.aes_key, "base64").toString("utf8");
  assert.equal(decoded, keyHex);
  assert.match(decoded, /^[0-9a-f]{32}$/);
});

test("sanitizeFileName strips directories and control characters", () => {
  assert.equal(sanitizeFileName("/etc/passwd"), "passwd");
  assert.equal(sanitizeFileName("..\\..\\win\\x.pdf"), "x.pdf");
  assert.equal(sanitizeFileName("a\u0000b\u001f.pdf"), "ab.pdf");
  assert.equal(sanitizeFileName("...hidden"), "hidden");
  assert.equal(sanitizeFileName(""), "");
  assert.equal(sanitizeFileName(undefined), "");
});

test("resolveSendableFile rejects missing paths", () => {
  assert.throws(() => resolveSendableFile("/nonexistent/nope.pdf"), /file not found/);
});

test("resolveSendableFile rejects directories", () => {
  assert.throws(() => resolveSendableFile(os.tmpdir()), /not a regular file/);
});

test("resolveSendableFile rejects empty files", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sidecar-test-"));
  const empty = path.join(dir, "empty.bin");
  fs.writeFileSync(empty, "");
  try {
    assert.throws(() => resolveSendableFile(empty), /empty file/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("resolveSendableFile enforces the size cap", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sidecar-test-"));
  const big = path.join(dir, "big.bin");
  // WECHAT_MAX_FILE_MB is pinned to 1 at the top of this file.
  fs.writeFileSync(big, Buffer.alloc(1024 * 1024 + 16, 7));
  try {
    assert.throws(() => resolveSendableFile(big), /too large/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("resolveSendableFile resolves symlinks and reports size", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sidecar-test-"));
  const real = path.join(dir, "real.txt");
  const link = path.join(dir, "link.txt");
  fs.writeFileSync(real, "12345");
  fs.symlinkSync(real, link);
  try {
    const resolved = resolveSendableFile(link);
    assert.equal(resolved.path, fs.realpathSync(real));
    assert.equal(resolved.size, 5);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("collectFileItems excludes images, keeps files and videos", () => {
  const msg = {
    item_list: [
      { type: 1, text_item: { text: "看这个" } },
      { type: 2, image_item: {} },
      { type: 3, voice_item: { text: "hi" } },
      { type: 4, file_item: {} },
      { type: 5, video_item: {} },
    ],
  };
  // Images (type 2) are split out so they ride pi's native `@file` multimodal
  // input instead of the "已收藏" file ack; only files/videos stay here.
  assert.deepEqual(collectFileItems(msg).map((item) => item.type), [4, 5]);
});

test("collectImageItems picks out only images", () => {
  const msg = {
    item_list: [
      { type: 1, text_item: { text: "看这个" } },
      { type: 2, image_item: {} },
      { type: 3, voice_item: { text: "hi" } },
      { type: 4, file_item: {} },
      { type: 2, image_item: {} },
      { type: 5, video_item: {} },
    ],
  };
  assert.deepEqual(collectImageItems(msg).map((item) => item.type), [2, 2]);
});

test("collectFileItems tolerates a missing item_list", () => {
  assert.deepEqual(collectFileItems({}), []);
});

test("collectImageItems tolerates a missing item_list", () => {
  assert.deepEqual(collectImageItems({}), []);
});

test("extractFilePayload resolves image_item", () => {
  const payload = extractFilePayload({
    type: 2,
    image_item: {
      media: { full_url: "https://novac2c.cdn.weixin.qq.com/c2c/download?x=1", aes_key: "k" },
      file_name: "photo.jpg",
      len: "20480",
    },
  });

  assert.equal(payload.name, "photo.jpg");
  assert.equal(payload.size, 20480);
  assert.equal(payload.aesKey, "k");
});

test("describeFileItem keeps field names but masks credentials", () => {
  const item = {
    type: 4,
    msg_id: "v1:123",
    file_item: {
      media: {
        encrypt_query_param: "SECRET-CDN-PARAM",
        aes_key: "U0VDUkVULUFFUw==",
        full_url: "https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=SECRET",
      },
      file_name: "季度报表.pdf",
      md5: "d6b0de98",
      len: "188728",
    },
  };

  const described = JSON.stringify(describeFileItem(item));

  // Field names are the whole point of the log, so they must survive.
  assert.match(described, /encrypt_query_param/);
  assert.match(described, /aes_key/);
  assert.match(described, /media_keys/);
  // The values must not: together they allow downloading and decrypting the file.
  assert.ok(!described.includes("SECRET-CDN-PARAM"));
  assert.ok(!described.includes("U0VDUkVULUFFUw=="));
  assert.ok(!described.includes("novac2c"));
  // Non-secret metadata stays useful for debugging.
  assert.equal(describeFileItem(item).file_name, "季度报表.pdf");
  assert.equal(describeFileItem(item).len, "188728");
  assert.equal(describeFileItem(item).credential_present, true);
});

test("describeFileItem handles an unrecognised item shape", () => {
  const described = describeFileItem({ type: 9 });

  assert.equal(described.type, 9);
  assert.deepEqual(described.payload_keys, []);
  assert.equal(described.credential_present, false);
});

test("module import does not start the CLI", () => {
  // Reaching this point means the direct-run guard held under node --test.
  assert.equal(typeof sidecar.buildFileItem, "function");
});
