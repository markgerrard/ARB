# Bus registrar operator procedures

The bus registrar is a single-instance service. Install and run exactly one
`arb-bus-registrar.service`; do not convert it to a templated `@` unit or run a
second provisioner concurrently. The credentials-file update and Valkey ACL
load are designed for one writer. The per-host sealing-key pin independently
continues to prevent key substitution.

## Legitimate machine re-key

Unpin is an operator-local database action and has no bus-envelope handler.
Run it on the registrar host against the local store:

```sh
bus-registrar-approve --unpin <host>
```

The command atomically deletes that host's `host_sealing_pins` row and appends
`host_sealing_key_unpinned` to `bus_operator_audit`, including the host, UTC
timestamp, and `operator-cli` source. It also emits the same structured audit
record to the operator's terminal/log.

Complete a re-key in this order:

1. Stop new approvals for the host and run the local `--unpin` command.
2. Mint a new host-scoped registration token.
3. Have the machine acknowledge again using its new sealing key.
4. Inspect and approve that pending request.

The successful provision re-establishes TOFU for the new key. Keep the re-key
request pending until its exact request ID is explicitly approved.

## Declared roles and additive acknowledgements

The client must declare the roles this host actually runs:

```sh
bus-register --roles codex,worker ...
```

Accepted names are `claude`, `codex`, `pi`, and `worker`. The signed request
binds the declaration, and `bus-registrar-approve --list` shows it before the
operator decides. A first acknowledgement creates only those identities.

A later approval for the same host and pinned sealing key may declare an
additional role. Provisioning adds the missing identity without rotating
existing credentials or changing the TOFU pin, and the sealed bundle contains
the host's full current role union. Declaring a subset never revokes roles;
role removal requires a separate future revoke flow. Restricting the
host-scoped registration token itself to a role subset is also deferred as a
further least-privilege tightening.
