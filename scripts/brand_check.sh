#!/usr/bin/env bash
# BRAND-NEUTRAL-01, the CRM half. No user of Procam software ever sees
# the name of an AI provider or model. To the user it is Procam software.
#
# PROCAM-DECISIONS-02NOV §9.4 asks for this run across both repos with
# zero hits before go-live. The TMS has had it since the sweep; the CRM
# had only `scripts/rebrand_procam_ai.py`, which FIXES text and does not
# answer "is it clean now" — so a release could be checked in one repo
# and merely hoped about in the other.
#
# What it scans is what reaches somebody: templates, the JavaScript in
# them, the Python that writes a message, a print or a mail. Comments in
# a template ship to the browser, so they count.
#
#   bash scripts/brand_check.sh          report
#   bash scripts/brand_check.sh --strict fail the build on any hit
set -uo pipefail
cd "$(dirname "$0")/.."

TERMS=scripts/ai_terms.txt
ALLOW=scripts/ai_terms_allow.txt
STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

# Where a vendor name is allowed to live.
#
#  * the adapter that calls the vendor's own SDK — §4 of the spec allows
#    a provider name in server-side configuration, and an adapter cannot
#    be written without naming the SDK it imports;
#  * this checker and its own term list;
#  * the spec document that has to quote the words it bans;
#  * tests, which assert the banned words do not appear and therefore
#    contain them;
#  * minified third-party bundles: lucide ships icons called "palm" and
#    "bard", which are a tree and a poet.
# The adapters that call a vendor's own SDK, and the two modules whose
# job is to know the vendor names. Each is server-side and none of them
# writes a user-visible string containing one -- which is the thing this
# check is for, and the reason the list is paths and not 53 whitelisted
# lines: a whitelist of lines passes again after somebody edits the line
# into a flash message.
EXCLUDE='scripts/ai_terms|scripts/brand_check|scripts/rebrand_procam_ai'
EXCLUDE="$EXCLUDE|app/copilot/model\.py|app/copilot/retrieval\.py"
EXCLUDE="$EXCLUDE|app/services/lead_intake_ai\.py|app/services/business_card_ocr\.py"
EXCLUDE="$EXCLUDE|app/services/ai_branding\.py|app/services/config_audit\.py"
EXCLUDE="$EXCLUDE|/node_modules/|\.git/|^tests/|/tests/|\.min\.js|docs/specs/BRAND"

hits=$(grep -rniE -f "$TERMS" \
        --include=*.html --include=*.js --include=*.py --include=*.jinja \
        --include=*.txt --include=*.md \
        app/ templates/ app.py 2>/dev/null \
      | grep -vE "$EXCLUDE" || true)

if [ -f "$ALLOW" ]; then
  # Comments and blanks are stripped first. `grep -vFf` treats every
  # line of the file as a pattern, and an empty pattern matches every
  # line -- so one blank line in the allow file silently passes the
  # whole check.
  allow=$(mktemp)
  grep -v '^[[:space:]]*#' "$ALLOW" | sed '/^[[:space:]]*$/d' > "$allow"
  if [ -s "$allow" ]; then
    hits=$(echo "$hits" | grep -vFf "$allow" || true)
  fi
  rm -f "$allow"
fi

hits=$(echo "$hits" | sed '/^$/d')

if [ -z "$hits" ]; then
  echo "brand check: clean — no AI provider or model name reaches a user."
  exit 0
fi

echo "brand check: $(echo "$hits" | wc -l | tr -d ' ') hit(s) that could reach a user:"
echo "$hits"
echo
echo "Fix each, or add the exact line to scripts/ai_terms_allow.txt if it is"
echo "a genuine customer name (for example a company called Gemini)."
[ "$STRICT" = "1" ] && exit 1
exit 0
