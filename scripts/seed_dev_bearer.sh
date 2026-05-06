#!/usr/bin/env bash
#
# Seed a dev user + active MCP bearer via psql (no Python heredoc, no
# multi-line paste pitfalls). Idempotent: re-running is safe; the user
# and the bearer are upserted via ON CONFLICT DO NOTHING.
#
# Usage:
#   bash scripts/seed_dev_bearer.sh
#
# Hardcoded plaintext: "dev-bearer-please-rotate-before-prod"
# SHA-256 hex:         f452507d0f59bcf725a0b459bbf718bf974149f3bf6d75a5bcd0087d16bcac6a
# Email:               franco.dev@sandinas.test
#
# After seeding, use the plaintext as Authorization: Bearer in curl:
#
#   curl -H "Authorization: Bearer dev-bearer-please-rotate-before-prod" \
#        http://localhost:8000/api/transcriptions ...
#
# To rotate (revoke + new): bump the plaintext + hash here and re-run.
# The current row stays alive (revoked_at IS NULL); the new INSERT trips
# the partial UNIQUE on uq_mcp_bearers_active_per_user. Acceptable for
# dev — for prod, run an UPDATE ... revoked_at = now() first.

set -euo pipefail

PLAINTEXT="dev-bearer-please-rotate-before-prod"
TOKEN_HASH="f452507d0f59bcf725a0b459bbf718bf974149f3bf6d75a5bcd0087d16bcac6a"
EMAIL="franco.dev@sandinas.test"
DISPLAY_NAME="Franco Dev"

docker compose exec -T postgres psql -U transcription -d transcription_api -v ON_ERROR_STOP=1 <<SQL
INSERT INTO users (id, microsoft_oid, email, display_name, created_at)
VALUES (gen_random_uuid(), gen_random_uuid(), '${EMAIL}', '${DISPLAY_NAME}', now())
ON CONFLICT (email) DO NOTHING;

INSERT INTO mcp_bearers (id, user_id, token_hash, name, created_at)
SELECT gen_random_uuid(), u.id, '${TOKEN_HASH}', 'dev-seed', now()
FROM users u
WHERE u.email = '${EMAIL}'
ON CONFLICT (token_hash) DO NOTHING;

SELECT u.id AS user_id, u.email, b.token_hash, b.name, b.revoked_at
FROM users u
JOIN mcp_bearers b ON b.user_id = u.id
WHERE u.email = '${EMAIL}';
SQL

echo ""
echo "==============================================="
echo "  Bearer plaintext (use as Authorization header):"
echo "  ${PLAINTEXT}"
echo "==============================================="
