import CryptoJS from "crypto-js";
import LZString from "lz-string";

export function encodeRoomPayload(value, dateString) {
  const digest = CryptoJS.MD5(dateString).toString();
  const key = CryptoJS.enc.Utf8.parse(digest.slice(0, 16));
  const iv = CryptoJS.enc.Utf8.parse(digest.slice(8, 14));
  const encrypted = CryptoJS.AES.encrypt(JSON.stringify(value), key, {
    iv,
    mode: CryptoJS.mode.CBC,
    padding: CryptoJS.pad.Pkcs7,
  }).toString();
  return LZString.compress(encrypted);
}
