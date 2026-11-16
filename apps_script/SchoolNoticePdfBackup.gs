/**
 * Owner-account PDF backup endpoint for school notices.
 *
 * SETUP (Google Apps Script):
 * 1. Project Settings -> Script properties:
 *    DESTINATION_FOLDER_ID = your private Drive folder ID.
 * 2. Run createUploadToken() once and copy the secret from the execution log
 *    directly into GitHub Secret NOTICE_UPLOAD_TOKEN. Never commit it.
 * 3. Deploy -> New deployment -> Web app:
 *    Execute as: Me; Who has access: Anyone.
 * 4. Put the /exec URL in GitHub Secret NOTICE_UPLOAD_WEBAPP_URL.
 *
 * SECURITY: This web app can be called without signing in, so the long random
 * secret is mandatory. Never reveal the URL/token pair to other people.
 */
const MAX_PDF_BYTES = 6 * 1024 * 1024;
const MAX_REQUEST_CHARS = 9 * 1024 * 1024;

function createUploadToken() {
  const props = PropertiesService.getScriptProperties();
  // Re-running intentionally rotates the token, invalidating the old one.
  const token = Utilities.getUuid().replace(/-/g, '') +
                Utilities.getUuid().replace(/-/g, '');
  props.setProperty('NOTICE_UPLOAD_TOKEN', token);
  console.log('NOTICE_UPLOAD_TOKEN (copy to GitHub secret; do not share): ' + token);
}

function jsonResponse(data) {
  return ContentService
    .createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}

function fixedCompare(a, b) {
  a = String(a || '');
  b = String(b || '');
  let mismatch = a.length ^ b.length;
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    mismatch |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return mismatch === 0;
}

function doPost(e) {
  try {
    const props = PropertiesService.getScriptProperties();
    const required = props.getProperty('NOTICE_UPLOAD_TOKEN');
    const folderId = props.getProperty('DESTINATION_FOLDER_ID');
    if (!required || required.length < 50 || !folderId) {
      return jsonResponse({ok:false, code:'SERVER_NOT_CONFIGURED'});
    }

    const raw = e && e.postData && e.postData.contents;
    if (!raw || raw.length > MAX_REQUEST_CHARS) {
      return jsonResponse({ok:false, code:'INVALID_SIZE'});
    }
    let p;
    try { p = JSON.parse(raw); }
    catch (_) { return jsonResponse({ok:false, code:'BAD_JSON'}); }
    if (!fixedCompare(p.token, required)) {
      return jsonResponse({ok:false, code:'UNAUTHORIZED'});
    }
    if (!p.content_b64 || !/^[A-Za-z0-9+/=]+$/.test(p.content_b64)) {
      return jsonResponse({ok:false, code:'BAD_BASE64'});
    }
    // Decode only after authenticating: unknown callers cannot force large work.
    if (p.content_b64.length > Math.ceil(MAX_PDF_BYTES * 4 / 3) + 8) {
      return jsonResponse({ok:false, code:'PDF_TOO_LARGE'});
    }
    const bytes = Utilities.base64Decode(p.content_b64);
    if (bytes.length < 5 || bytes.length > MAX_PDF_BYTES ||
        bytes[0] !== 37 || bytes[1] !== 80 || bytes[2] !== 68 || bytes[3] !== 70) {
      return jsonResponse({ok:false, code:'NOT_PDF'});
    }

    const sha = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes)
      .map(b => ('0' + (b & 255).toString(16)).slice(-2)).join('');
    if (typeof p.sha256 !== 'string' || !fixedCompare(sha, p.sha256.toLowerCase())) {
      return jsonResponse({ok:false, code:'SHA256_MISMATCH'});
    }
    const cleanId = String(p.notice_id || '').replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 36);
    const cleanName = String(p.filename || 'notice').replace(/[^a-zA-Z0-9가-힣._-]/g, '_')
      .replace(/\.pdf$/i, '').slice(0, 55);
    const finalName = (cleanId || 'notice') + '_' + sha.slice(0, 20) +
      '_' + (cleanName || 'attachment') + '.pdf';

    // Unique name from original bytes makes retries idempotent.
    const lock = LockService.getScriptLock();
    if (!lock.tryLock(25000)) {
      return jsonResponse({ok:false, code:'BUSY_RETRY_LATER'});
    }
    try {
      const folder = DriveApp.getFolderById(folderId);
      const existing = folder.getFilesByName(finalName);
      if (existing.hasNext()) {
        const item = existing.next();
        return jsonResponse({ok:true, status:'ALREADY_SAVED',
                             id:item.getId(), url:item.getUrl()});
      }
      const blob = Utilities.newBlob(bytes, 'application/pdf', finalName);
      const item = folder.createFile(blob);
      return jsonResponse({ok:true, status:'SAVED', id:item.getId(), url:item.getUrl()});
    } finally {
      lock.releaseLock();
    }
  } catch (_) {
    // Do not return PDF text, user data, tokens, private URLs or exceptions.
    return jsonResponse({ok:false, code:'SERVER_ERROR'});
  }
}

function doGet(_) {
  return jsonResponse({ok:false, code:'POST_REQUIRED'});
}
