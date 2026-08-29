from __future__ import annotations


def setup_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arb_messages (
            id                bigserial PRIMARY KEY,
            agent_id          text NOT NULL,
            request_id        text NOT NULL,
            request_type      text NOT NULL,
            provider          text NOT NULL,
            capability        text NOT NULL,
            status            text NOT NULL DEFAULT 'pending',
            policy_decision   text,
            policy_reason     text,
            reason            text,
            provider_token_id text,
            body              bytea,
            attempts          int NOT NULL DEFAULT 0,
            claimed_at        timestamptz,
            completed_at      timestamptz,
            delivered_at      timestamptz,
            token_revoked_at  timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now(),
            UNIQUE (agent_id, request_id)
        )
        """
    )
    conn.execute("ALTER TABLE arb_messages ADD COLUMN IF NOT EXISTS policy_decision text")
    conn.execute("ALTER TABLE arb_messages ADD COLUMN IF NOT EXISTS policy_reason text")
    conn.execute("ALTER TABLE arb_messages ADD COLUMN IF NOT EXISTS reason text")
    conn.execute("ALTER TABLE arb_messages ADD COLUMN IF NOT EXISTS provider_token_id text")
    conn.execute("ALTER TABLE arb_messages ADD COLUMN IF NOT EXISTS body bytea")
    conn.execute("ALTER TABLE arb_messages ADD COLUMN IF NOT EXISTS attempts int NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE arb_messages ADD COLUMN IF NOT EXISTS claimed_at timestamptz")
    conn.execute("ALTER TABLE arb_messages ADD COLUMN IF NOT EXISTS completed_at timestamptz")
    conn.execute("ALTER TABLE arb_messages ADD COLUMN IF NOT EXISTS delivered_at timestamptz")
    conn.execute("ALTER TABLE arb_messages ADD COLUMN IF NOT EXISTS token_revoked_at timestamptz")
    conn.execute("CREATE INDEX IF NOT EXISTS arb_messages_status_claimed_at_idx ON arb_messages (status, claimed_at)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arb_agent_keys (
            agent_id    text NOT NULL,
            pubkey      text NOT NULL,
            fingerprint text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            revoked_at  timestamptz
        )
        """
    )
    conn.execute("ALTER TABLE arb_agent_keys ADD COLUMN IF NOT EXISTS fingerprint text NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE arb_agent_keys ADD COLUMN IF NOT EXISTS revoked_at timestamptz")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS arb_agent_keys_one_live_key_idx
        ON arb_agent_keys (agent_id) WHERE revoked_at IS NULL
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arb_messages_settings (
            key   text PRIMARY KEY,
            value text NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO arb_messages_settings (key, value)
        VALUES ('paused', '0')
        ON CONFLICT (key) DO NOTHING
        """
    )
