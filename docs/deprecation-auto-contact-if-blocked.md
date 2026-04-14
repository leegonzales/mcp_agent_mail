# Deprecation: `auto_contact_if_blocked`

**Status:** Deprecated as of `fix/step-2-shadow-create-root-cause` (2026-04).
**Removal target:** one release after deprecation.

## Why

Silent handshake-on-block erodes the contact-discipline the server is meant
to enforce. Callers should establish contact links explicitly via
`macro_contact_handshake` so that approval state is intentional and
auditable, not a side-effect of a send.

## Migration

Before:
```python
send_message(..., auto_contact_if_blocked=True)
```

After:
```python
macro_contact_handshake(
    project_key=...,
    requester=sender,
    target=recipient,
    to_project=<recipient project_key>,
    auto_accept=False,  # True only if you own both sides
)
# ...then:
send_message(..., to_project=<recipient project_key>)
```

## Timeline

- **Now:** `auto_contact_if_blocked=True` logs a `WARNING` but still works.
- **Next release:** parameter removed. Callers using it will get a
  `TypeError: send_message() got an unexpected keyword argument
  'auto_contact_if_blocked'`.
