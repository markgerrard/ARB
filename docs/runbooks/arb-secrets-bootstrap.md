# ARB Secrets Bootstrap

ARB Secrets peers use a local X25519 keypair and publish only the public key to the Redis bus.
Initialize a peer before sending or claiming secrets:

```sh
PYTHONPATH="$(pwd)/src" python -m arb_secrets.cli init
PYTHONPATH="$(pwd)/src" python -m arb_secrets.cli publish "$AGENT_ID" --redis-url "$ARB_DEV_REDIS_URL"
```

The private key is stored at `~/.arb-secrets/privkey.b64` with mode `600`. The publish command prints
the public-key fingerprint; operators should vouch that fingerprint out of band on first contact.
Peer pins are stored in `~/.arb-secrets/known_peers.b64` and are not auto-updated on mismatch.
