"""What this platform has pushed into JSM Assets, and where it got to.

Revision ID: 0072
Revises: 0071

ONE WAY, ALWAYS. Inventory flows DCIM -> Assets and never back. The DCIM is
the system of record for physical infrastructure - it is the thing wired to
the equipment - and a CMDB that could write back into rack elevations is a
data corruption vector wearing a synchronisation label. NetBox Labs made the
same call for the same reason and says so plainly in their own docs.

WHY `type_map` IS A CACHE AND NOT A CONVENIENCE. Everything in the Assets API
is addressed by NUMERIC ATTRIBUTE ID - `objectTypeAttributeId: "135"` - never
by name. Without a cached map from "Serial number" to 135, every object pushed
needs a walk of `GET .../objecttype/{id}/attributes` first, which turns a
1,500-device sync into 1,500 extra round trips against an API that rate-limits
external imports separately from everything else. This is the single biggest
source of friction in any Assets integration and the cache is the whole answer.

WHY `last_cursor` IS A TIMESTAMP AND NOT A ROW COUNT. The export is
incremental on `device.updated_at`, so the cursor is a high-water mark. It is
deliberately NOT advanced until a run reports completion: a run that dies
half-way must re-send the devices it had already pushed rather than skip them,
because Assets imports are idempotent on the object key and re-sending is
free, while skipping loses a machine until somebody touches it again.
"""

from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE assets_sync_state (
            integration_id uuid PRIMARY KEY
                           REFERENCES integration(id) ON DELETE CASCADE,

            -- Discovered once from GET /rest/servicedeskapi/assets/workspace.
            -- Its absence is how "this site has no Assets" is detected, which
            -- is a licence fact (Premium or Enterprise) rather than a
            -- configuration mistake, and the UI says so in those words.
            workspace_id   text,
            schema_id      text,

            -- {"Device": {"id": "23", "attributes": {"Name": "135", ...}}}
            -- See the module docstring: everything in the Assets API is
            -- addressed by numeric id, never by name.
            type_map       jsonb NOT NULL DEFAULT '{}'::jsonb,

            -- The import configuration's own credential, encrypted with the
            -- same key as everything else. A SECOND credential, because the
            -- Imports REST API is authenticated by a token the operator
            -- creates against one import source in Jira rather than by the
            -- account credential the rest of this integration uses.
            import_token_enc bytea,
            import_id      text,

            -- High-water mark on device.updated_at. Advanced only when a run
            -- REPORTS COMPLETION, so a run that dies half way re-sends rather
            -- than skips - re-sending is free and idempotent, skipping loses a
            -- machine until somebody touches it again.
            last_cursor    timestamptz,
            last_run_at    timestamptz,
            last_run_status text,
            last_error     text,
            objects_pushed integer NOT NULL DEFAULT 0,
            runs           integer NOT NULL DEFAULT 0
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS assets_sync_state")
