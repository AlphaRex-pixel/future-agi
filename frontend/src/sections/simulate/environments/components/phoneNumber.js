const MIN_DIGITS = 10;
const MAX_DIGITS = 12;
const MAX_NATIONAL_DIGITS = 10;
const KEEPS_TRUNK_ZERO = new Set(["39"]);
const FIXED_NATIONAL_DIGITS = { 1: 10, 91: 10 };

export function nationalDigits(dial, number) {
  const raw = String(number || "").trim();
  const local = raw.replace(/\D/g, "");
  if (raw.startsWith("+")) return local;
  const dialDigits = String(dial || "").replace(/\D/g, "");
  return local.startsWith("0") && !KEEPS_TRUNK_ZERO.has(dialDigits) ? local.slice(1) : local;
}

export function phoneNumberError(dial, number) {
  const raw = String(number || "").trim();
  if (!raw) return null;
  const dialDigits = raw.startsWith("+") ? "" : String(dial || "").replace(/\D/g, "");
  const national = nationalDigits(dial, raw);
  const total = dialDigits.length + national.length;
  const typedCode = raw.startsWith("+") ? ["91", "1"].find((code) => national.startsWith(code)) : null;
  const code = typedCode || dialDigits;
  const fixed = FIXED_NATIONAL_DIGITS[code];
  if (fixed) {
    const length = typedCode ? national.length - typedCode.length : national.length;
    return length === fixed ? null : `Enter exactly ${fixed} digits after +${code}`;
  }
  if (raw.startsWith("+")) {
    if (total < MIN_DIGITS || total > MAX_DIGITS) {
      return `Enter ${MIN_DIGITS} to ${MAX_DIGITS} digits including the country code`;
    }
    return null;
  }
  if (national.length > MAX_NATIONAL_DIGITS) {
    return `Enter at most ${MAX_NATIONAL_DIGITS} digits after the country code`;
  }
  if (total < MIN_DIGITS) {
    return `Enter at least ${MIN_DIGITS - dialDigits.length} digits`;
  }
  if (total > MAX_DIGITS) {
    return `Enter at most ${MAX_DIGITS - dialDigits.length} digits`;
  }
  return null;
}

export const isValidPhoneNumber = (dial, number) =>
  Boolean(String(number || "").trim()) && phoneNumberError(dial, number) === null;
